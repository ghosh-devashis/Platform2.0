"""Tests for the manifest (TPL-02), the scaffolder and CLI (TPL-01), metadata (SDK-14), seeding (RUN-04), identity (SDK-06)."""

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import boto3
import pytest
from botocore.stub import Stubber
from fastapi.testclient import TestClient
from langchain_core.language_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from ent_agent_sdk import identity, manifest, metadata
from ent_agent_sdk.cli import main
from ent_agent_sdk.devtools import scaffold, seed

REPO = Path(__file__).resolve().parents[2]
GOOD = {"name": "crm-agent", "team": "sales", "data_classification": "confidential",
        "models": ["chat-default"], "tools": ["lookup"], "secrets": ["crm/api"]}


def test_valid_manifest_parses_with_defaults():
    parsed = manifest.parse(GOOD)
    assert parsed.guardrail_profile == "standard"
    assert parsed.runtime_name == "crm_agent"
    assert manifest.parse({**GOOD, "data_classification": "restricted", "guardrail_profile": "strict"}).guardrail_profile == "strict"


@pytest.mark.parametrize("change, expected", [
    ({"name": "Bad_Name"}, "'name'"),
    ({"team": ""}, "'team'"),
    ({"data_classification": "secret"}, "'data_classification'"),
    ({"data_classification": "restricted"}, "strict"),
    ({"guardrail_profile": "lenient"}, "'guardrail_profile'"),
    ({"models": "chat-default"}, "'models'"),
    ({"tools": ["a", "a"]}, "duplicates"),
    ({"surprise": 1}, "Unknown key"),
])
def test_invalid_manifests_list_every_problem(change, expected):
    with pytest.raises(manifest.ManifestError) as info:
        manifest.parse({**GOOD, **change})
    assert expected in str(info.value)


def test_a_generated_agents_manifest_is_valid(tmp_path):
    scaffold.create_project("probe-agent", team="platform", directory=tmp_path)
    assert manifest.load(tmp_path / "probe-agent" / "agent.yaml").name == "probe-agent"


def test_the_templates_models_are_in_the_approved_list(tmp_path):
    approved = set(json.loads((REPO / "policy/data/approved-models.json").read_text())["approved_models"])
    registry = set(json.loads((REPO / "harness/model-router/registry.json").read_text())["models"])
    assert approved == registry
    scaffold.create_project("probe-agent", team="platform", directory=tmp_path)
    generated = manifest.load(tmp_path / "probe-agent" / "agent.yaml")
    assert generated.models and set(generated.models) <= approved


def test_scaffolded_project_has_expected_files_and_a_valid_manifest(tmp_path):
    written = scaffold.create_project("crm-agent", team="sales", classification="restricted", directory=tmp_path)
    root = tmp_path / "crm-agent"
    for relative in ("pyproject.toml", "agent.yaml", "CLAUDE.md", "AGENTS.md", ".github/copilot-instructions.md",
                     ".github/workflows/ci.yml", ".pre-commit-config.yaml", ".gitignore", "Dockerfile", "compose.yaml",
                     "waivers.yaml", "src/crm_agent/agent.py", "src/crm_agent/__main__.py", "tests/test_agent.py"):
        assert (root / relative).is_file(), relative
    assert not any("__NAME__" in p.read_text() or "__PACKAGE__" in p.read_text() for p in written)
    loaded = manifest.load(root / "agent.yaml")
    assert (loaded.name, loaded.team, loaded.guardrail_profile) == ("crm-agent", "sales", "strict")
    assert "from crm_agent.agent import create_app" in (root / "src/crm_agent/__main__.py").read_text()


def test_scaffold_rejects_bad_names_and_non_empty_targets(tmp_path):
    with pytest.raises(scaffold.ScaffoldError):
        scaffold.create_project("Bad Name", team="t", directory=tmp_path)
    scaffold.create_project("ok-agent", team="t", directory=tmp_path)
    with pytest.raises(scaffold.ScaffoldError):
        scaffold.create_project("ok-agent", team="t", directory=tmp_path)


def test_cli_new_and_validate(tmp_path, capsys):
    assert main(["new", "demo-agent", "--team", "platform", "--dir", str(tmp_path)]) == 0
    assert main(["manifest", "validate", str(tmp_path / "demo-agent" / "agent.yaml")]) == 0
    assert "is valid" in capsys.readouterr().out
    assert main(["manifest", "validate", str(tmp_path / "nope.yaml")]) == 1


def test_scaffolded_agent_runs_end_to_end(tmp_path, monkeypatch):
    """The generated agent module works with a scripted model (what the generated test does)."""
    scaffold.create_project("e2e-agent", team="t", directory=tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path / "e2e-agent" / "src"))
    monkeypatch.setenv("AGENT_MANIFEST", str(tmp_path / "e2e-agent" / "agent.yaml"))
    from importlib import import_module

    agent = import_module("e2e_agent.agent")

    class Fake(GenericFakeChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

    model = Fake(messages=iter([AIMessage(content="hello from the template")]))
    response = TestClient(agent.create_app(model).asgi).post("/invocations", json={"prompt": "hi"})
    assert response.json() == {"output": "hello from the template"}


def test_metadata_collects_and_round_trips(tmp_path, monkeypatch):
    monkeypatch.setenv("ENT_POLICY_VERSION", "0.1.0")
    monkeypatch.setenv("ENT_GIT_SHA", "abc1234")
    path = tmp_path / "meta.json"
    written = metadata.write(path)
    assert written["policy_bundle_version"] == "0.1.0"
    assert metadata.read(path)["git_sha"] == "abc1234"
    assert written["frameworks"]["langgraph"]
    monkeypatch.setenv("ENT_METADATA_PATH", str(path))
    assert metadata.resource_attributes()["ent.policy.version"] == "0.1.0"


class FakeAws:
    """Stub AWS session for seeding: Secrets Manager and AgentCore control plane."""

    def __init__(self, existing_secrets=(), existing_runtimes=()):
        self.created_secrets, self.created_runtimes = [], []
        self.existing_secrets, self.existing_runtimes = set(existing_secrets), list(existing_runtimes)

    def client(self, service, **_):
        outer = self

        class Secrets:
            def describe_secret(self, SecretId):
                if SecretId not in outer.existing_secrets:
                    from botocore.exceptions import ClientError
                    raise ClientError({"Error": {"Code": "ResourceNotFoundException"}}, "DescribeSecret")

            def create_secret(self, Name, SecretString, Description):
                outer.created_secrets.append((Name, SecretString))

        class Control:
            def list_agent_runtimes(self):
                return {"agentRuntimes": outer.existing_runtimes}

            def create_agent_runtime(self, **kwargs):
                outer.created_runtimes.append(kwargs)
                return {"agentRuntimeArn": "arn:aws:bedrock-agentcore:us-east-1:000000000000:runtime/x-AbCdEfGh12"}

        return Secrets() if service == "secretsmanager" else Control()


def test_seed_creates_secrets_and_runtime_without_overwriting():
    fake = FakeAws(existing_secrets={"llm/anthropic"})
    result = seed.seed(manifest.parse(GOOD), session=fake, endpoint="http://localhost:4566")
    created = {name for name, _ in fake.created_secrets}
    assert created == {"crm/api", "llm/openai"}
    assert result["secrets_kept"] == ["llm/anthropic"]
    assert all(value == seed.PLACEHOLDER for _, value in fake.created_secrets)
    request = fake.created_runtimes[0]
    assert request["agentRuntimeName"] == "crm_agent"  # no hyphens: AgentCore forbids them
    assert request["networkConfiguration"] == {"networkMode": "PUBLIC"}


def test_seed_uses_env_values_and_is_idempotent_for_runtimes(monkeypatch):
    monkeypatch.setenv("ENT_SEED_VALUE_CRM_API", "real-value")
    fake = FakeAws(existing_runtimes=[{"agentRuntimeName": "crm_agent", "agentRuntimeArn": "arn:x"}])
    result = seed.seed(manifest.parse({**GOOD, "models": []}), session=fake, endpoint="http://localhost:4566")
    assert ("crm/api", "real-value") in fake.created_secrets
    assert fake.created_runtimes == []
    assert result["runtime"]["arn"] == "arn:x"


def test_seed_refuses_non_local_endpoints():
    with pytest.raises(seed.SeedError):
        seed.seed(manifest.parse(GOOD), session=FakeAws(), endpoint="https://secretsmanager.us-east-1.amazonaws.com")


def _session():
    return boto3.session.Session(aws_access_key_id="t", aws_secret_access_key="t", region_name="us-east-1")


def test_agentcore_identity_gets_and_caches_short_lived_tokens():
    provider = identity.AgentCoreIdentity(credential_provider="crm-oauth", workload_name="crm-agent", session=_session())
    client = provider._agentcore()
    stub = Stubber(client)
    stub.add_response("get_workload_access_token", {"workloadAccessToken": "wl-token"}, {"workloadName": "crm-agent"})
    stub.add_response(
        "get_resource_oauth2_token", {"accessToken": "tok-1"},
        {"workloadIdentityToken": "wl-token", "resourceCredentialProviderName": "crm-oauth",
         "scopes": ["crm.read"], "oauth2Flow": "M2M"},
    )
    stub.activate()
    identity.configure(provider)
    assert identity.bearer_headers(["crm.read"]) == {"Authorization": "Bearer tok-1"}
    assert identity.bearer_headers(["crm.read"]) == {"Authorization": "Bearer tok-1"}  # cached: no further calls
    stub.assert_no_pending_responses()


def test_identity_errors_never_contain_tokens():
    provider = identity.AgentCoreIdentity(credential_provider="crm-oauth", workload_name="crm-agent", session=_session())
    stub = Stubber(provider._agentcore())
    stub.add_client_error("get_workload_access_token", service_error_code="AccessDeniedException")
    stub.activate()
    with pytest.raises(identity.IdentityError, match="AccessDeniedException"):
        provider.get_token(["x"])


def test_bearer_headers_need_a_configured_provider(monkeypatch):
    monkeypatch.setattr(identity, "_provider", None)
    with pytest.raises(identity.IdentityError):
        identity.bearer_headers()


def test_sts_identity_returns_and_caches_credentials():
    provider = identity.StsIdentity(role_arn="arn:aws:iam::123456789012:role/tool", session=_session())
    sts = _session().client("sts")
    provider._session = type("S", (), {"client": lambda self, *a, **k: sts})()
    stub = Stubber(sts)
    expiry = datetime.now(timezone.utc) + timedelta(minutes=15)
    stub.add_response(
        "assume_role",
        {"Credentials": {"AccessKeyId": "AKIAEXAMPLE0000000", "SecretAccessKey": "s", "SessionToken": "t", "Expiration": expiry}},
        {"RoleArn": "arn:aws:iam::123456789012:role/tool", "RoleSessionName": "ent-agent", "DurationSeconds": 900},
    )
    stub.activate()
    first = provider.credentials()
    assert first["aws_session_token"] == "t"
    assert provider.credentials() == first  # cached
    stub.assert_no_pending_responses()
