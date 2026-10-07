"""Identity (SDK-06): short-lived credentials for outbound calls. No static keys in code or config.

Two providers:

- `AgentCoreIdentity`: OAuth 2.0 access tokens for tools and APIs, issued by Amazon Bedrock AgentCore Identity
  (machine-to-machine flow) using the agent's workload identity.
- `StsIdentity`: short-lived AWS credentials (STS AssumeRole) for calls to AWS services or SigV4 APIs.

    identity.configure(identity.AgentCoreIdentity(credential_provider="crm-oauth"))
    headers = identity.bearer_headers(scopes=["crm.read"])        # {"Authorization": "Bearer <token>"}

Tokens are cached briefly and refreshed before they expire. Failures raise `IdentityError`; the message never
contains a token. Written against the AgentCore API reference (GetWorkloadAccessToken, GetResourceOauth2Token); the
local Floci emulator may not implement these operations, so verify against a real AWS account before relying on it.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from ent_agent_sdk.secrets import BOTO_CONFIG

REFRESH_MARGIN_SECONDS = 60.0
DEFAULT_TOKEN_TTL_SECONDS = 300.0


class IdentityError(RuntimeError):
    """A credential could not be obtained."""


@dataclass(frozen=True)
class AccessToken:
    token: str
    expires_at: float  # time.monotonic() deadline

    def valid(self) -> bool:
        return self.expires_at - REFRESH_MARGIN_SECONDS > time.monotonic()


class TokenProvider(Protocol):
    def get_token(self, scopes: Sequence[str] = (), audience: str | None = None) -> AccessToken: ...


class AgentCoreIdentity:
    """OAuth 2.0 tokens (client-credentials / M2M) from AgentCore Identity for this agent's workload identity."""

    def __init__(
        self,
        *,
        credential_provider: str,
        workload_name: str | None = None,
        session: Any = None,
        ttl_seconds: float = DEFAULT_TOKEN_TTL_SECONDS,
    ) -> None:
        self.credential_provider = credential_provider
        self.workload_name = workload_name
        self.ttl_seconds = ttl_seconds
        self._session = session
        self._client: Any = None
        self._cache: dict[tuple[tuple[str, ...], str | None], AccessToken] = {}
        self._lock = threading.Lock()

    def _agentcore(self) -> Any:
        if self._client is None:
            self._client = (self._session or boto3.session.Session()).client("bedrock-agentcore", config=BOTO_CONFIG)
        return self._client

    def get_token(self, scopes: Sequence[str] = (), audience: str | None = None) -> AccessToken:
        key = (tuple(scopes), audience)
        with self._lock:
            cached = self._cache.get(key)
        if cached and cached.valid():
            return cached
        try:
            client = self._agentcore()
            workload = client.get_workload_access_token(workloadName=self.workload_name)["workloadAccessToken"]
            request: dict[str, Any] = {
                "workloadIdentityToken": workload,
                "resourceCredentialProviderName": self.credential_provider,
                "scopes": list(scopes),
                "oauth2Flow": "M2M",
            }
            if audience:
                request["audiences"] = [audience]
            response = client.get_resource_oauth2_token(**request)
        except (BotoCoreError, ClientError, KeyError) as exc:
            code = exc.response.get("Error", {}).get("Code") if isinstance(exc, ClientError) else type(exc).__name__
            raise IdentityError(f"Could not get a token from credential provider '{self.credential_provider}' ({code}).") from None
        token = response.get("accessToken")
        if not token:  # e.g. the provider asked for user authorization, which an agent can't give on its own
            raise IdentityError(f"Credential provider '{self.credential_provider}' returned no token.")
        result = AccessToken(token, time.monotonic() + self.ttl_seconds)
        with self._lock:
            self._cache[key] = result
        return result


class StsIdentity:
    """Short-lived AWS credentials from STS AssumeRole, refreshed before they expire."""

    def __init__(self, *, role_arn: str, session_name: str = "ent-agent", duration_seconds: int = 900, session: Any = None) -> None:
        self.role_arn = role_arn
        self.session_name = session_name
        self.duration_seconds = duration_seconds
        self._session = session
        self._credentials: dict[str, Any] | None = None
        self._expires_at = 0.0
        self._lock = threading.Lock()

    def credentials(self) -> dict[str, str]:
        """`aws_access_key_id`, `aws_secret_access_key`, `aws_session_token` for boto3."""
        with self._lock:
            if self._credentials is None or self._expires_at - REFRESH_MARGIN_SECONDS <= time.monotonic():
                try:
                    sts = (self._session or boto3.session.Session()).client("sts", config=BOTO_CONFIG)
                    response = sts.assume_role(
                        RoleArn=self.role_arn, RoleSessionName=self.session_name, DurationSeconds=self.duration_seconds
                    )
                except (BotoCoreError, ClientError) as exc:
                    code = exc.response.get("Error", {}).get("Code") if isinstance(exc, ClientError) else type(exc).__name__
                    raise IdentityError(f"Could not assume role '{self.role_arn}' ({code}).") from None
                creds = response["Credentials"]
                self._credentials = {
                    "aws_access_key_id": creds["AccessKeyId"],
                    "aws_secret_access_key": creds["SecretAccessKey"],
                    "aws_session_token": creds["SessionToken"],
                }
                lifetime = min(self.duration_seconds, max((creds["Expiration"].timestamp() - time.time()), 0.0))
                self._expires_at = time.monotonic() + lifetime
            return dict(self._credentials)

    def boto3_session(self, region_name: str | None = None) -> boto3.session.Session:
        return boto3.session.Session(region_name=region_name, **self.credentials())


_provider: TokenProvider | None = None


def configure(provider: TokenProvider) -> None:
    """Set the token provider used by `bearer_headers`."""
    global _provider
    _provider = provider


def bearer_headers(scopes: Sequence[str] = (), audience: str | None = None) -> dict[str, str]:
    """`{"Authorization": "Bearer <short-lived token>"}` for an outbound API call."""
    if _provider is None:
        raise IdentityError("No identity provider configured. Call identity.configure(...) at startup.")
    return {"Authorization": f"Bearer {_provider.get_token(scopes, audience).token}"}
