"""Secrets (SDK-05): fetch secrets at runtime from AWS Secrets Manager or SSM Parameter Store.

Agents never read secrets from code, env vars or files; they call `secrets.get("name")`. Where secrets come
from is configuration, not code, so the same image works locally and in AWS (RUN-02):

- Credentials and region: the standard AWS chain (an IAM role in AgentCore; dummy keys locally).
- Endpoint: the standard `AWS_ENDPOINT_URL` setting. Set it to http://localhost:4566 for Floci; leave it
  unset in AWS.

Fail closed (NFR-03): if a secret can't be fetched, `SecretsError` is raised. There is no default value.
Error messages name the secret but never contain its value.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger("ent_agent_sdk.secrets")

SSM_PREFIX = "ssm:"
DEFAULT_TTL_SECONDS = 300.0

# Resilience (SDK-16): short timeouts and a few retries with backoff, then a clear error.
BOTO_CONFIG = Config(connect_timeout=2, read_timeout=5, retries={"mode": "standard", "max_attempts": 3})

_NOT_FOUND_CODES = {"ResourceNotFoundException", "ParameterNotFound"}


class SecretsError(RuntimeError):
    """A secret could not be fetched or read."""


class SecretNotFoundError(SecretsError):
    """The secret does not exist (or the caller may not know it exists)."""


class SecretsClient:
    """Fetches secrets with a time-limited cache.

    Names are Secrets Manager secret names/ARNs, or `ssm:/path/to/parameter` for SSM Parameter Store
    (SecureString parameters are decrypted). Cached values expire after `ttl_seconds`, so rotated secrets are
    picked up automatically; pass `refresh=True` to fetch immediately (e.g. after an auth failure).
    """

    def __init__(self, *, ttl_seconds: float = DEFAULT_TTL_SECONDS, session: Any = None) -> None:
        self.ttl_seconds = ttl_seconds
        self._session = session
        self._clients: dict[str, Any] = {}
        self._cache: dict[str, tuple[str, float]] = {}
        self._lock = threading.Lock()

    def get(self, name: str, *, key: str | None = None, refresh: bool = False) -> str:
        """Return the secret's value, or one field of a JSON secret if `key` is given."""
        if not name:
            raise ValueError("Secret name must not be empty.")
        value = self._cached(name) if not refresh else None
        if value is None:
            value = self._fetch(name)
            with self._lock:
                self._cache[name] = (value, time.monotonic() + self.ttl_seconds)
        return value if key is None else _field(name, value, key)

    async def aget(self, name: str, *, key: str | None = None, refresh: bool = False) -> str:
        """Async version of `get` for async graph nodes (runs the AWS call in a worker thread)."""
        return await asyncio.to_thread(self.get, name, key=key, refresh=refresh)

    def clear_cache(self) -> None:
        with self._lock:
            self._cache.clear()

    def _cached(self, name: str) -> str | None:
        with self._lock:
            entry = self._cache.get(name)
        if entry and entry[1] > time.monotonic():
            return entry[0]
        return None

    def _client(self, service: str) -> Any:
        with self._lock:
            if service not in self._clients:
                session = self._session or boto3.session.Session()
                self._clients[service] = session.client(service, config=BOTO_CONFIG)
            return self._clients[service]

    def _fetch(self, name: str) -> str:
        logger.debug("Fetching secret %s", name)
        try:
            if name.startswith(SSM_PREFIX):
                response = self._client("ssm").get_parameter(Name=name[len(SSM_PREFIX):], WithDecryption=True)
                return response["Parameter"]["Value"]
            response = self._client("secretsmanager").get_secret_value(SecretId=name)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "Unknown")
            if code in _NOT_FOUND_CODES:
                raise SecretNotFoundError(f"Secret '{name}' was not found.") from None
            raise SecretsError(f"Could not fetch secret '{name}' ({code}).") from None
        except BotoCoreError as exc:  # no credentials, endpoint unreachable, timeouts after retries
            raise SecretsError(f"Could not fetch secret '{name}' ({type(exc).__name__}).") from None

        if "SecretString" in response:
            return response["SecretString"]
        try:
            return response["SecretBinary"].decode("utf-8")
        except (KeyError, UnicodeDecodeError):
            raise SecretsError(f"Secret '{name}' has no text value.") from None


def _field(name: str, value: str, key: str) -> str:
    try:
        data = json.loads(value)
    except json.JSONDecodeError:
        raise SecretsError(f"Secret '{name}' is not JSON, so key '{key}' can't be read.") from None
    if not isinstance(data, dict) or key not in data:
        raise SecretsError(f"Secret '{name}' has no key '{key}'.")
    field = data[key]
    return field if isinstance(field, str) else json.dumps(field)


_default_client: SecretsClient | None = None
_default_lock = threading.Lock()


def default_client() -> SecretsClient:
    global _default_client
    with _default_lock:
        if _default_client is None:
            _default_client = SecretsClient()
        return _default_client


def get(name: str, *, key: str | None = None, refresh: bool = False) -> str:
    """Fetch a secret using the shared client. See `SecretsClient.get`."""
    return default_client().get(name, key=key, refresh=refresh)


async def aget(name: str, *, key: str | None = None, refresh: bool = False) -> str:
    """Async version of `get`."""
    return await default_client().aget(name, key=key, refresh=refresh)


def clear_cache() -> None:
    default_client().clear_cache()
