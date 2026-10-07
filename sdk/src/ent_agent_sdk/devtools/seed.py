"""Seed the local stack from an agent manifest (RUN-04): a fresh Floci matches `agent.yaml` with no manual steps.

Creates (and never overwrites):
- every secret the manifest lists, plus the provider-key secrets the model router reads, as clearly marked
  placeholders. Real values come from `ENT_SEED_VALUE_<NAME>` environment variables (name upper-cased, `/` and `-`
  as `_`), or you replace them afterwards;
- the agent's AgentCore runtime in Floci's control plane, so deployment scripts and the invoke router find it.

Local development only: refuses to run against a non-local endpoint unless `allow_remote=True`.
"""

from __future__ import annotations

import os
import re
from typing import Any
from urllib.parse import urlparse

import boto3
from botocore.exceptions import ClientError

from ent_agent_sdk.manifest import AgentManifest

DEFAULT_ENDPOINT = "http://localhost:4566"
DEFAULT_LLM_SECRETS = ("llm/anthropic", "llm/openai")
DEFAULT_CHECKPOINT_TABLE = "agent-checkpoints"
PLACEHOLDER = "dev-placeholder-replace-me"
LOCAL_ROLE_ARN = "arn:aws:iam::000000000000:role/local-agent-role"
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "floci", "host.docker.internal"}


class SeedError(RuntimeError):
    pass


def _env_name(secret: str) -> str:
    return "ENT_SEED_VALUE_" + re.sub(r"[^A-Za-z0-9]", "_", secret).upper()


def _session(region: str) -> boto3.session.Session:
    return boto3.session.Session(
        aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID", "test"),
        aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY", "test"),
        region_name=region,
    )


def seed(
    manifest: AgentManifest,
    *,
    endpoint: str | None = None,
    region: str | None = None,
    image: str | None = None,
    llm_secrets: tuple[str, ...] = DEFAULT_LLM_SECRETS,
    allow_remote: bool = False,
    session: Any = None,
) -> dict[str, Any]:
    """Create the manifest's resources in Floci. Returns what was created, kept and registered."""
    endpoint = endpoint or os.environ.get("AWS_ENDPOINT_URL", DEFAULT_ENDPOINT)
    region = region or os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
    if not allow_remote and (urlparse(endpoint).hostname or "") not in _LOCAL_HOSTS:
        raise SeedError(f"Refusing to seed '{endpoint}': not a local endpoint. (Seeding is for local development.)")
    session = session or _session(region)

    summary: dict[str, Any] = {"secrets_created": [], "secrets_kept": [], "runtime": None, "resources_created": [],
                               "resources_kept": []}

    secrets_client = session.client("secretsmanager", endpoint_url=endpoint)
    wanted = list(manifest.secrets) + [s for s in llm_secrets if manifest.models and s not in manifest.secrets]
    for name in wanted:
        try:
            secrets_client.describe_secret(SecretId=name)
            summary["secrets_kept"].append(name)
            continue
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ResourceNotFoundException":
                raise SeedError(f"Could not check secret '{name}': {exc.response['Error']['Code']}") from None
        value = os.environ.get(_env_name(name))
        secrets_client.create_secret(
            Name=name, SecretString=value or PLACEHOLDER,
            Description=f"Seeded for agent {manifest.name}" + ("" if value else " (PLACEHOLDER - replace with a real value)"),
        )
        summary["secrets_created"].append(name)

    control = session.client("bedrock-agentcore-control", endpoint_url=endpoint)
    _seed_resources(manifest, session, control, endpoint, summary)
    try:
        existing = {
            r["agentRuntimeName"]: r for r in control.list_agent_runtimes().get("agentRuntimes", [])
        }
        runtime = existing.get(manifest.runtime_name)
        if runtime is None:
            runtime = control.create_agent_runtime(
                agentRuntimeName=manifest.runtime_name,
                agentRuntimeArtifact={"containerConfiguration": {"containerUri": image or f"local/{manifest.name}:dev"}},
                roleArn=LOCAL_ROLE_ARN,
                networkConfiguration={"networkMode": "PUBLIC"},
                protocolConfiguration={"serverProtocol": "HTTP"},
                description=manifest.description or f"{manifest.name} ({manifest.team})",
                tags={"team": manifest.team, "data-classification": manifest.data_classification},
            )
            summary["runtime_created"] = True
        summary["runtime"] = {"name": manifest.runtime_name, "arn": runtime.get("agentRuntimeArn")}
    except ClientError as exc:
        raise SeedError(f"Could not register the AgentCore runtime: {exc.response['Error']['Code']}") from None
    return summary

def _seed_resources(manifest: AgentManifest, session: Any, control: Any, endpoint: str, summary: dict[str, Any]) -> None:
    """Buckets, tables, the session-memory table and AgentCore memories the manifest declares (all idempotent)."""

    def record(kind: str, name: str, created: bool) -> None:
        summary["resources_created" if created else "resources_kept"].append(f"{kind}:{name}")

    resources = manifest.resources
    s3 = session.client("s3", endpoint_url=endpoint)
    for bucket in resources.get("buckets", []):
        try:
            s3.head_bucket(Bucket=bucket)
            record("bucket", bucket, False)
        except ClientError:
            try:
                s3.create_bucket(Bucket=bucket)
            except ClientError as exc:
                raise SeedError(f"Could not create bucket '{bucket}': {exc.response['Error']['Code']}") from None
            record("bucket", bucket, True)

    tables = list(resources.get("tables", []))
    if manifest.memory.get("backend") == "dynamodb":  # the session-memory table (see ent_agent_sdk.memory)
        tables.append({"name": manifest.memory.get("table") or DEFAULT_CHECKPOINT_TABLE, "partition_key": "thread_id", "sort_key": "sk"})
    dynamodb = session.client("dynamodb", endpoint_url=endpoint)
    for table in tables:
        try:
            dynamodb.describe_table(TableName=table["name"])
            record("table", table["name"], False)
            continue
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ResourceNotFoundException":
                raise SeedError(f"Could not check table '{table['name']}': {exc.response['Error']['Code']}") from None
        keys = [{"AttributeName": table["partition_key"], "KeyType": "HASH"}]
        attributes = [{"AttributeName": table["partition_key"], "AttributeType": "S"}]
        if table.get("sort_key"):
            keys.append({"AttributeName": table["sort_key"], "KeyType": "RANGE"})
            attributes.append({"AttributeName": table["sort_key"], "AttributeType": "S"})
        try:
            dynamodb.create_table(TableName=table["name"], KeySchema=keys, AttributeDefinitions=attributes, BillingMode="PAY_PER_REQUEST")
        except ClientError as exc:
            raise SeedError(f"Could not create table '{table['name']}': {exc.response['Error']['Code']}") from None
        record("table", table["name"], True)

    memories = resources.get("memories", [])
    if memories:
        try:
            existing = {m["id"].rsplit("-", 1)[0] for m in control.list_memories().get("memories", [])}
            for memory in memories:
                if memory in existing:
                    record("memory", memory, False)
                else:
                    control.create_memory(name=memory, eventExpiryDuration=30)
                    record("memory", memory, True)
        except ClientError as exc:
            raise SeedError(f"Could not register AgentCore memory: {exc.response['Error']['Code']}") from None
