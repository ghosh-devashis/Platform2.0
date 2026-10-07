"""Dependency health checks for `/ping` (SDK-16): report Unhealthy honestly when the LLM gateway is unreachable.

    EnterpriseAgentApp(graph, name="a", health_check=health.gateway_check())

`EnterpriseAgentApp.from_manifest` installs this check by default when the manifest lists models (turn it off with
`ENT_HEALTH_CHECK_GATEWAY=false`). A check never raises and is cached for a few seconds, so health probes stay cheap
and don't hammer the gateway.
"""

from __future__ import annotations

import logging
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from urllib.parse import urlsplit, urlunsplit

logger = logging.getLogger("ent_agent_sdk.health")

DEFAULT_GATEWAY_URL = "http://localhost:8788/v1"


def _gateway_base(url: str) -> str:
    parts = urlsplit(url)
    path = parts.path.rstrip("/")
    if path.endswith("/v1"):
        path = path[: -len("/v1")]
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def gateway_reachable(url: str | None = None, timeout: float = 1.0) -> bool:
    """True if something answers at the gateway: any HTTP response below 500 counts, connection errors don't."""
    base = _gateway_base(url or os.environ.get("ENT_MODEL_GATEWAY_URL", DEFAULT_GATEWAY_URL))
    for path in ("/health", "/"):
        try:
            with urllib.request.urlopen(base + path, timeout=timeout) as response:  # noqa: S310 (configured URL)
                return response.status < 500
        except urllib.error.HTTPError as exc:
            if exc.code < 500 and exc.code != 404:
                return True
            if exc.code >= 500:
                return False
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            return False
    return True  # /health and / both answered 404: the server is up


def gateway_check(url: str | None = None, *, ttl: float = 10.0, timeout: float = 1.0) -> Callable[[], bool]:
    """A cached health check for `EnterpriseAgentApp(health_check=...)`."""
    state: dict[str, float | bool] = {"at": float("-inf"), "ok": True}

    def check() -> bool:
        now = time.monotonic()
        if now - float(state["at"]) >= ttl:
            ok = gateway_reachable(url, timeout)
            if not ok and state["ok"]:
                logger.warning("LLM gateway is unreachable; reporting Unhealthy")
            state.update(at=now, ok=ok)
        return bool(state["ok"])

    return check
