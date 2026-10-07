"""Seeding buckets, tables, memories and the session-memory table from a manifest, against the real Floci (RUN-04)."""

import socket
import uuid

import boto3
import pytest

from ent_agent_sdk import manifest
from ent_agent_sdk.devtools import seed

ENDPOINT = "http://localhost:4566"


def _floci_running() -> bool:
    try:
        socket.create_connection(("localhost", 4566), timeout=0.5).close()
        return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(not _floci_running(), reason="Floci is not running on localhost:4566")


def test_seed_creates_every_declared_resource_and_is_idempotent():
    tag = uuid.uuid4().hex[:8]
    session = boto3.session.Session(aws_access_key_id="test", aws_secret_access_key="test", region_name="us-east-1")
    parsed = manifest.parse({
        "name": f"seedtest-{tag}", "team": "t", "data_classification": "internal",
        "secrets": [f"seedtest/{tag}"],
        "resources": {"buckets": [f"seedtest-{tag}-files"], "tables": [{"name": f"seedtest-{tag}-notes", "partition_key": "id", "sort_key": "ts"}],
                      "memories": [f"seedtest_{tag}"]},
        "memory": {"backend": "dynamodb", "table": f"seedtest-{tag}-checkpoints"},
    })
    s3, ddb = session.client("s3", endpoint_url=ENDPOINT), session.client("dynamodb", endpoint_url=ENDPOINT)
    control = session.client("bedrock-agentcore-control", endpoint_url=ENDPOINT)
    sm = session.client("secretsmanager", endpoint_url=ENDPOINT)
    try:
        first = seed.seed(parsed, session=session, endpoint=ENDPOINT)
        assert set(first["resources_created"]) == {f"bucket:seedtest-{tag}-files", f"table:seedtest-{tag}-notes",
                                                   f"table:seedtest-{tag}-checkpoints", f"memory:seedtest_{tag}"}
        s3.head_bucket(Bucket=f"seedtest-{tag}-files")
        notes = ddb.describe_table(TableName=f"seedtest-{tag}-notes")["Table"]
        assert [k["KeyType"] for k in notes["KeySchema"]] == ["HASH", "RANGE"]
        checkpoints = ddb.describe_table(TableName=f"seedtest-{tag}-checkpoints")["Table"]
        assert [k["AttributeName"] for k in checkpoints["KeySchema"]] == ["thread_id", "sk"]  # what ent_agent_sdk.memory expects
        assert any(m["id"].startswith(f"seedtest_{tag}-") for m in control.list_memories()["memories"])

        second = seed.seed(parsed, session=session, endpoint=ENDPOINT)
        assert second["resources_created"] == [] and len(second["resources_kept"]) == 4
        assert second["secrets_created"] == []
    finally:
        for call in (
            lambda: s3.delete_bucket(Bucket=f"seedtest-{tag}-files"),
            lambda: ddb.delete_table(TableName=f"seedtest-{tag}-notes"),
            lambda: ddb.delete_table(TableName=f"seedtest-{tag}-checkpoints"),
            lambda: [control.delete_memory(memoryId=m["id"]) for m in control.list_memories()["memories"] if m["id"].startswith(f"seedtest_{tag}-")],
            lambda: sm.delete_secret(SecretId=f"seedtest/{tag}", ForceDeleteWithoutRecovery=True),
            lambda: [control.delete_agent_runtime(agentRuntimeId=r["agentRuntimeId"]) for r in control.list_agent_runtimes()["agentRuntimes"]
                     if r["agentRuntimeName"] == f"seedtest_{tag}"],
        ):
            try:
                call()
            except Exception:
                pass  # best-effort cleanup of throwaway resources
