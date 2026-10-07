"""Model access (SDK-07, GW-01, GW-04): approved models by logical name, always through the LLM gateway.

    from ent_agent_sdk.models import get_model
    llm = get_model()                        # the model agent.yaml declares: a LangChain chat model
                                             # (supports .invoke, .bind_tools, streaming)

The model is chosen in one place, `models:` in `agent.yaml`: `get_model()` returns the first entry, so the name is
never repeated in agent code. Pass a name (`get_model("chat-fast")`) only when the agent really uses several models;
`declared_models()` lists them in the order agent.yaml gives them.

The model talks to the gateway's OpenAI-compatible API (locally the model router in front of Portkey), never
to a provider directly. Which provider/model sits behind a logical name is gateway configuration, so it can
change without touching agent code. Unapproved names are rejected by the gateway with a clear error.

Every request carries correlation headers so gateway logs join the agent's trace (GW-04):
`x-portkey-trace-id` (the OpenTelemetry trace ID), `x-portkey-metadata` (agent, version, team, environment)
and W3C `traceparent`.

Resilience (SDK-16): the SDK does not retry LLM calls (`max_retries=0`); retries and fallbacks are the
gateway's job, so failures don't multiply into retry storms.

Configuration: `ENT_MODEL_GATEWAY_URL` (default http://localhost:8788/v1) and `ENT_MODEL_GATEWAY_KEY`
(the agent's gateway credential, not a provider key).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from langchain_openai import ChatOpenAI
from openai import DefaultAsyncHttpxClient, DefaultHttpxClient
from opentelemetry import propagate, trace

from ent_agent_sdk import context as ent_context
from ent_agent_sdk import manifest as manifest_mod
from ent_agent_sdk import observability as obs

GATEWAY_URL_ENV = "ENT_MODEL_GATEWAY_URL"
GATEWAY_KEY_ENV = "ENT_MODEL_GATEWAY_KEY"
DEFAULT_GATEWAY_URL = "http://localhost:8788/v1"
DEFAULT_TIMEOUT_SECONDS = 120.0

# Settings agents may not override: they would bypass the gateway or its credentials (GW-01).
_LOCKED = {"base_url", "openai_api_base", "api_key", "openai_api_key", "http_client", "http_async_client",
           "max_retries", "openai_proxy", "default_headers"}


class ModelConfigError(ValueError):
    """get_model() was called with settings that would bypass the gateway."""


def declared_models(manifest: str | Path | None = None) -> tuple[str, ...]:
    """The logical model names under `models:` in the agent's manifest, in the order agent.yaml lists them.

    The manifest is `manifest` if given, else `AGENT_MANIFEST`, else `agent.yaml` in the working directory.
    """
    path = Path(manifest) if manifest else manifest_mod.default_path()
    try:
        loaded = manifest_mod.load(path)
    except manifest_mod.ManifestError as exc:
        raise ModelConfigError(
            f"Cannot read the model from the manifest ({'; '.join(exc.problems)}). Run the agent from its project folder, "
            f"set {manifest_mod.MANIFEST_ENV} to the agent.yaml path, or pass the name: get_model(\"chat-default\")."
        ) from None
    if not loaded.models:
        raise ModelConfigError(f"{path} lists no models. Add one under models:, for example `models: [chat-default]`.")
    return loaded.models


def get_model(name: str | None = None, *, manifest: str | Path | None = None,
              timeout: float = DEFAULT_TIMEOUT_SECONDS, **kwargs: Any) -> ChatOpenAI:
    """Return a chat model, routed via the gateway.

    With no `name` the model is the first one listed under `models:` in `agent.yaml` (looked up as in
    `declared_models`), so the choice lives in the manifest and not in code. Pass a logical name (e.g. "chat-fast")
    only for an agent that uses several models. Extra keyword arguments (e.g. `temperature`, `max_tokens`) are
    passed to the model.
    """
    if name is None:
        name = declared_models(manifest)[0]
    if not name:
        raise ModelConfigError("Model name must not be empty.")
    locked = _LOCKED.intersection(kwargs)
    if locked:
        raise ModelConfigError(f"get_model() does not allow overriding {sorted(locked)}; all traffic goes through the gateway.")
    return ChatOpenAI(
        model=name,
        base_url=os.environ.get(GATEWAY_URL_ENV, DEFAULT_GATEWAY_URL),
        api_key=os.environ.get(GATEWAY_KEY_ENV, "local-dev"),
        timeout=timeout,
        max_retries=0,
        http_client=DefaultHttpxClient(event_hooks={"request": [_add_correlation_headers]}),
        http_async_client=DefaultAsyncHttpxClient(event_hooks={"request": [_aadd_correlation_headers]}),
        **kwargs,
    )


def correlation_headers() -> dict[str, str]:
    """Headers linking a gateway request to the current agent trace, plus the agent's guardrail profile and data
    classification so the gateway can apply the matching guardrails (GW-05) and caching rules (GW-09)."""
    headers: dict[str, str] = {}
    invocation = ent_context.current()
    if invocation is not None:
        if invocation.guardrails is not None and getattr(invocation.guardrails, "enabled", True):
            headers["x-ent-guardrail-profile"] = invocation.guardrails.profile
        if invocation.data_classification:
            headers["x-ent-data-classification"] = invocation.data_classification
    span = trace.get_current_span()
    context = span.get_span_context()
    if not context.is_valid:
        return headers
    headers["x-portkey-trace-id"] = format(context.trace_id, "032x")
    attributes = getattr(span, "attributes", None) or {}
    metadata = {
        key: str(attributes[attribute])
        for key, attribute in (("agent", obs.AGENT_NAME), ("agent_version", obs.AGENT_VERSION), ("team", obs.TEAM))
        if attribute in attributes
    }
    resource = getattr(span, "resource", None)
    environment = resource.attributes.get("deployment.environment") if resource is not None else None
    if environment:
        metadata["environment"] = str(environment)
    if metadata:
        headers["x-portkey-metadata"] = json.dumps(metadata)
    propagate.inject(headers)
    return headers


def _add_correlation_headers(request: Any) -> None:
    request.headers.update(correlation_headers())


async def _aadd_correlation_headers(request: Any) -> None:
    request.headers.update(correlation_headers())
