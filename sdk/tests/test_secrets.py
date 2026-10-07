"""Tests for secrets.get() (SDK-05). Unit tests stub AWS; the integration test uses Floci if it's running."""

import json
import socket
import uuid

import boto3
import pytest
from botocore.config import Config
from botocore.stub import Stubber

from ent_agent_sdk.secrets import SecretNotFoundError, SecretsClient, SecretsError

FLOCI_ENDPOINT = "http://localhost:4566"


def _session() -> boto3.session.Session:
    return boto3.session.Session(aws_access_key_id="test", aws_secret_access_key="test", region_name="us-east-1")


@pytest.fixture
def stubbed():
    """A SecretsClient whose Secrets Manager and SSM calls are answered by stubs."""
    client = SecretsClient(session=_session())
    sm, ssm = Stubber(client._client("secretsmanager")), Stubber(client._client("ssm"))
    with sm, ssm:
        yield client, sm, ssm
        sm.assert_no_pending_responses()
        ssm.assert_no_pending_responses()


def test_get_returns_secret_string(stubbed):
    client, sm, _ = stubbed
    sm.add_response("get_secret_value", {"SecretString": "s3cret"}, {"SecretId": "app/db"})
    assert client.get("app/db") == "s3cret"


def test_get_reads_key_from_json_secret(stubbed):
    client, sm, _ = stubbed
    sm.add_response("get_secret_value", {"SecretString": json.dumps({"user": "bob", "port": 5432})})
    assert client.get("app/db", key="user") == "bob"
    assert client.get("app/db", key="port") == "5432"  # second call served from cache


def test_get_caches_until_refresh(stubbed):
    client, sm, _ = stubbed
    sm.add_response("get_secret_value", {"SecretString": "v1"})
    sm.add_response("get_secret_value", {"SecretString": "v2"})
    assert client.get("app/key") == "v1"
    assert client.get("app/key") == "v1"
    assert client.get("app/key", refresh=True) == "v2"


def test_expired_cache_fetches_again(stubbed):
    client, sm, _ = stubbed
    client.ttl_seconds = 0
    sm.add_response("get_secret_value", {"SecretString": "v1"})
    sm.add_response("get_secret_value", {"SecretString": "v2 (rotated)"})
    assert client.get("app/key") == "v1"
    assert client.get("app/key") == "v2 (rotated)"


def test_get_reads_ssm_parameter(stubbed):
    client, _, ssm = stubbed
    ssm.add_response(
        "get_parameter",
        {"Parameter": {"Name": "/app/url", "Value": "https://example.test", "Type": "SecureString"}},
        {"Name": "/app/url", "WithDecryption": True},
    )
    assert client.get("ssm:/app/url") == "https://example.test"


def test_missing_secret_raises_not_found(stubbed):
    client, sm, _ = stubbed
    sm.add_client_error("get_secret_value", service_error_code="ResourceNotFoundException")
    with pytest.raises(SecretNotFoundError, match="'nope' was not found"):
        client.get("nope")


def test_access_denied_fails_closed(stubbed):
    client, sm, _ = stubbed
    sm.add_client_error("get_secret_value", service_error_code="AccessDeniedException")
    with pytest.raises(SecretsError, match="AccessDeniedException"):
        client.get("app/db")


def test_missing_key_raises_without_leaking_value(stubbed):
    client, sm, _ = stubbed
    sm.add_response("get_secret_value", {"SecretString": json.dumps({"password": "hunter2"})})
    with pytest.raises(SecretsError) as info:
        client.get("app/db", key="user")
    assert "hunter2" not in str(info.value)


def test_unreachable_endpoint_raises_secrets_error():
    client = SecretsClient(session=_session())
    fast_fail = Config(connect_timeout=0.2, retries={"max_attempts": 1})
    client._clients["secretsmanager"] = _session().client(
        "secretsmanager", endpoint_url="http://127.0.0.1:9", config=fast_fail
    )
    # Connection refused or timed out (depends on the OS): either way a clear SecretsError, never a default.
    with pytest.raises(SecretsError, match=r"Could not fetch secret 'app/db' \((EndpointConnection|ConnectTimeout)Error\)"):
        client.get("app/db")


def _floci_running() -> bool:
    try:
        socket.create_connection(("localhost", 4566), timeout=0.5).close()
        return True
    except OSError:
        return False


@pytest.mark.skipif(not _floci_running(), reason="Floci is not running on localhost:4566")
def test_get_against_floci(monkeypatch):
    """End to end: the SDK reads a real secret from Floci's Secrets Manager, configured only by endpoint."""
    monkeypatch.setenv("AWS_ENDPOINT_URL", FLOCI_ENDPOINT)
    admin = _session().client("secretsmanager", endpoint_url=FLOCI_ENDPOINT)
    name = f"sdk-test/{uuid.uuid4().hex[:8]}"
    admin.create_secret(Name=name, SecretString=json.dumps({"api_key": "from-floci"}))
    try:
        assert SecretsClient(session=_session()).get(name, key="api_key") == "from-floci"
    finally:
        admin.delete_secret(SecretId=name, ForceDeleteWithoutRecovery=True)
