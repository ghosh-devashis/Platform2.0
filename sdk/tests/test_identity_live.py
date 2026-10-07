"""StsIdentity against the real Floci STS (SDK-06). AgentCore Identity's token operations aren't implemented by Floci
(the control plane is, the data plane returns 404), so that path is covered by stubbed tests only."""

import json
import socket
import uuid

import boto3
import pytest

from ent_agent_sdk import identity

ENDPOINT = "http://localhost:4566"


def _floci_running() -> bool:
    try:
        socket.create_connection(("localhost", 4566), timeout=0.5).close()
        return True
    except OSError:
        return False


@pytest.mark.skipif(not _floci_running(), reason="Floci is not running on localhost:4566")
def test_sts_identity_gets_short_lived_credentials_from_floci(monkeypatch):
    monkeypatch.setenv("AWS_ENDPOINT_URL", ENDPOINT)
    session = boto3.session.Session(aws_access_key_id="test", aws_secret_access_key="test", region_name="us-east-1")
    iam = session.client("iam", endpoint_url=ENDPOINT)
    role = f"ent-identity-test-{uuid.uuid4().hex[:8]}"
    trust = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"AWS": "*"}, "Action": "sts:AssumeRole"}]}
    arn = iam.create_role(RoleName=role, AssumeRolePolicyDocument=json.dumps(trust))["Role"]["Arn"]
    try:
        provider = identity.StsIdentity(role_arn=arn, session_name="ent-agent-test", session=session)
        credentials = provider.credentials()
        assert set(credentials) == {"aws_access_key_id", "aws_secret_access_key", "aws_session_token"}
        assert all(credentials.values())
        assert provider.credentials() == credentials  # cached until shortly before expiry
        scoped = provider.boto3_session("us-east-1")
        assert scoped.get_credentials().token == credentials["aws_session_token"]
    finally:
        iam.delete_role(RoleName=role)