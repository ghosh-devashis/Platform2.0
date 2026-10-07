"""Model router: the local stand-in for Portkey's enterprise control plane (SDK-07, GW-02..GW-10, RUN-06).

The open-source Portkey gateway is stateless: every request must carry its routing config and provider key.
The router holds the approved model registry and the provider keys (from Secrets Manager), so agents only
send a logical model name:

    agent --(model: "chat-default")--> router :8788 --(x-portkey-config: provider, key, fallbacks, guardrails)--> Portkey

With Portkey enterprise, registry entries become saved Portkey configs, keys become virtual keys, and budgets, rate
limits, caching, guardrails and high availability are native; this router goes away and agent code does not change.

What the router does per request:
- authenticates the caller with its own gateway credential (`ROUTER_GATEWAY_KEYS`), never a provider key (GW-02)
- applies the caller's guardrail profile at the gateway: secrets (and, for `strict`, personal data) are blocked in
  prompts and answers before/after the provider call (GW-05)
- enforces a per-client rate limit and daily token budget (GW-07: `ROUTER_RATE_LIMIT_RPM`, `ROUTER_TOKEN_BUDGET_PER_DAY`)
- emits an OpenTelemetry span per call, joined to the agent's trace (GW-08, GW-04)
- caches identical non-sensitive requests for models that opt in (GW-09)
- fails over between gateway instances (`PORTKEY_URLS`) and never bypasses the gateway when all are down (GW-10)

Modes (`ROUTER_MODE`), all behind the same API so agent code is identical in each:
- `real`    route to the real providers through Portkey (default).
- `record`  like real, and save each answer to the recordings directory.
- `replay`  answer from recordings only: no keys, no gateway, no internet. A request with no recording gets a
            clearly marked stub answer (`ROUTER_REPLAY_MISSING=stub`, default) or a 404 (`=error`, used in CI).
- `local`   route to a local model (for example Ollama) through Portkey, using each registry entry's `local` block.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx2
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from opentelemetry import propagate
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import SpanKind, Status, StatusCode
from starlette.background import BackgroundTask

from ent_agent_sdk import observability as obs
from ent_agent_sdk.app import FASTAPI_TELEMETRY_OFF
from ent_agent_sdk.secrets import SecretsClient, SecretsError
from model_router.controls import GatewayKeys, RateLimiter, ResponseCache, TokenBudgets, UrlPool, parse_pairs
from model_router.recordings import RecordingStore, request_key

logger = logging.getLogger("model_router")

ROUTER_PORT = 8788
MODES = ("real", "record", "replay", "local")
DEFAULT_REGISTRY = Path(__file__).resolve().parents[2] / "registry.json"
DEFAULT_RECORDINGS = Path(__file__).resolve().parents[2] / "recordings"
DEFAULT_PORTKEY_URL = "http://localhost:8787/v1"
# Correlation headers the SDK sends (GW-04); passed through to Portkey unchanged.
PASSTHROUGH_HEADERS = ("x-portkey-trace-id", "x-portkey-metadata", "traceparent")
DEFAULT_CACHE_CLASSIFICATIONS = ["public", "internal"]


class LocalModelNotConfigured(LookupError):
    """The registry entry has no `local` block."""


class Registry:
    """Approved logical models and gateway guardrail profiles, loaded from registry.json."""

    def __init__(self, models: dict[str, dict[str, Any]], guardrail_profiles: dict[str, dict[str, Any]] | None = None) -> None:
        self.models = models
        # keys starting with "_" are comments in registry.json, not profiles
        self.guardrail_profiles = {k: v for k, v in (guardrail_profiles or {}).items() if not k.startswith("_")}

    @classmethod
    def load(cls, path: str | Path) -> Registry:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(data["models"], data.get("guardrail_profiles"))

    def names(self) -> list[str]:
        return sorted(self.models)

    def portkey_config(self, name: str, secrets: SecretsClient, mode: str = "real") -> dict[str, Any]:
        """Build the Portkey config for a logical model. In `real` mode this includes provider keys: never log it."""
        entry = self.models[name]
        config: dict[str, Any] = {}
        if entry.get("retry"):
            config["retry"] = entry["retry"]
        if entry.get("request_timeout_ms"):
            config["request_timeout"] = entry["request_timeout_ms"]

        if mode == "local":
            local = entry.get("local")
            if not local:
                raise LocalModelNotConfigured(name)
            config.update({"provider": "ollama", "custom_host": local["custom_host"],
                           "override_params": {"model": local["model"]}})
            return config

        targets = [
            {
                "provider": target["provider"],
                "api_key": secrets.get(target["key_secret"]),
                "override_params": {"model": target["model"], **target.get("params", {})},
            }
            for target in entry["targets"]
        ]
        if len(targets) == 1:
            config.update(targets[0])
        else:
            config["strategy"] = {"mode": entry.get("strategy", "fallback")}
            config["targets"] = targets
        return config


def _int_env(name: str) -> int:
    value = os.environ.get(name, "").strip()
    return int(value) if value else 0


def create_app(
    *,
    registry: Registry | None = None,
    portkey_url: str | list[str] | None = None,
    secrets: SecretsClient | None = None,
    http_client: httpx2.AsyncClient | None = None,
    mode: str | None = None,
    recordings: RecordingStore | None = None,
    replay_missing: str | None = None,
    gateway_keys: dict[str, str] | None = None,
    rate_limit_rpm: int | None = None,
    token_budget_per_day: int | None = None,
    tracer_provider: TracerProvider | None = None,
    cache: ResponseCache | None = None,
    url_pool: UrlPool | None = None,
) -> FastAPI:
    registry = registry or Registry.load(os.environ.get("ROUTER_REGISTRY", DEFAULT_REGISTRY))
    if url_pool is None:
        if portkey_url is None:
            urls = [u.strip() for u in os.environ.get("PORTKEY_URLS", "").split(",") if u.strip()] or [
                os.environ.get("PORTKEY_URL", DEFAULT_PORTKEY_URL)]
        else:
            urls = [portkey_url] if isinstance(portkey_url, str) else list(portkey_url)
        url_pool = UrlPool(urls)
    secrets = secrets or SecretsClient()
    client = http_client or httpx2.AsyncClient(timeout=httpx2.Timeout(300.0, connect=2.0))
    mode = mode or os.environ.get("ROUTER_MODE", "real")
    if mode not in MODES:
        raise ValueError(f"ROUTER_MODE must be one of {MODES}, got '{mode}'.")
    recordings = recordings or RecordingStore(os.environ.get("ROUTER_RECORDINGS_DIR", DEFAULT_RECORDINGS))
    replay_missing = replay_missing or os.environ.get("ROUTER_REPLAY_MISSING", "stub")
    if replay_missing not in ("stub", "error"):
        raise ValueError("ROUTER_REPLAY_MISSING must be 'stub' or 'error'.")
    keys = GatewayKeys(gateway_keys if gateway_keys is not None
                       else parse_pairs(os.environ.get("ROUTER_GATEWAY_KEYS", ""), "ROUTER_GATEWAY_KEYS"))
    limiter = RateLimiter(rate_limit_rpm if rate_limit_rpm is not None else _int_env("ROUTER_RATE_LIMIT_RPM"))
    budgets = TokenBudgets(token_budget_per_day if token_budget_per_day is not None else _int_env("ROUTER_TOKEN_BUDGET_PER_DAY"))
    response_cache = cache or ResponseCache()
    owns_tracer = tracer_provider is None
    provider = tracer_provider or obs.build_tracer_provider(service_name="model-router", service_version="0.1.0")
    tracer = provider.get_tracer("model_router")
    if not keys.enabled:
        logger.warning("ROUTER_GATEWAY_KEYS is not set: the router accepts any caller (fine for local development only)")

    @asynccontextmanager
    async def lifespan(_: FastAPI):  # noqa: ANN202
        yield
        if owns_tracer:
            provider.shutdown()  # flush buffered spans

    api = FastAPI(title="model-router", docs_url=None, redoc_url=None, telemetry=FASTAPI_TELEMETRY_OFF, lifespan=lifespan)

    @api.get("/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "mode": mode, "auth": keys.enabled, "gateways": len(url_pool.urls)}

    @api.get("/v1/models")
    async def models(request: Request) -> Response:
        if keys.authenticate(request.headers.get("authorization")) is None:
            return _unauthorized()
        return JSONResponse({
            "object": "list",
            "data": [
                {"id": name, "object": "model", "owned_by": "ent-model-registry",
                 "description": registry.models[name].get("description", "")}
                for name in registry.names()
            ],
        })

    async def handle(body: dict[str, Any], name: str, label: str, request: Request, span: Any) -> Response:
        entry = registry.models[name]
        wants_stream = bool(body.get("stream"))

        wait = limiter.check(label)
        if wait is not None:
            span.set_attribute("ent.router.limited", "rate")
            return _error(429, "rate_limit_error", "rate_limit_exceeded",
                          f"Rate limit exceeded for client '{label}'. Retry in {wait}s.", {"retry-after": str(wait)})
        if budgets.exceeded(label):
            span.set_attribute("ent.router.limited", "budget")
            return _error(429, "insufficient_quota", "budget_exceeded", f"The daily token budget for client '{label}' is used up.")

        if mode == "replay":
            key = request_key(body)
            recorded = recordings.load(key)
            if recorded is not None:
                logger.info("model=%s mode=replay hit=%s client=%s", name, key[:12], label)
                _note_answering_model(span, recorded)
                return _completion(recorded, wants_stream, {"x-router-replay": "hit"})
            if replay_missing == "error":
                logger.warning("model=%s mode=replay no recording for %s", name, key[:12])
                return _error(404, "invalid_request_error", "recording_not_found",
                              f"No recording for this request (key {key[:12]}). Record one with ROUTER_MODE=record.")
            logger.info("model=%s mode=replay stub (no recording for %s)", name, key[:12])
            return _completion(_stub(name, key), wants_stream, {"x-router-replay": "stub"})

        # Gateway guardrails for the caller's profile (GW-05).
        profile = request.headers.get("x-ent-guardrail-profile") or ("standard" if "standard" in registry.guardrail_profiles else None)
        hooks: dict[str, Any] = {}
        if registry.guardrail_profiles and profile is not None:
            if profile not in registry.guardrail_profiles:
                return _error(400, "invalid_request_error", "unknown_guardrail_profile",
                              f"Unknown guardrail profile '{profile}'. Known: {', '.join(sorted(registry.guardrail_profiles))}.")
            hooks = copy.deepcopy(registry.guardrail_profiles[profile])
            span.set_attribute("ent.guardrail.profile", profile)

        # Cache (GW-09): only models that opt in, only non-sensitive callers, only whole answers.
        classification = request.headers.get("x-ent-data-classification", "")
        cache_settings = entry.get("cache") or {}
        cacheable = bool(cache_settings) and not wants_stream and mode in ("real", "local") and \
            classification in cache_settings.get("classifications", DEFAULT_CACHE_CLASSIFICATIONS)
        cache_key = f"{mode}|{name}|{request_key(body)}|{body.get('temperature')}|{body.get('max_completion_tokens')}"
        if cacheable:
            cached = response_cache.get(cache_key)
            if cached is not None:
                span.set_attribute("ent.router.cache", "hit")
                _note_answering_model(span, cached)
                logger.info("model=%s cache=hit client=%s", name, label)
                return JSONResponse(cached, headers={"x-router-cache": "hit"})

        try:
            config = registry.portkey_config(name, secrets, mode)
        except LocalModelNotConfigured:
            return _error(503, "gateway_error", "local_model_not_configured",
                          f"Model '{name}' has no local model configured in the registry.")
        except SecretsError as exc:
            logger.error("Provider credentials unavailable for model %s: %s", name, exc)
            return _error(503, "gateway_error", "provider_credentials_unavailable",
                          f"Provider credentials for model '{name}' are unavailable.")
        config.update(hooks)

        upstream_body = body
        if mode == "record" and wants_stream:  # record whole answers; replay streams them back
            upstream_body = {k: v for k, v in body.items() if k not in ("stream", "stream_options")}
        headers = {"content-type": "application/json", "x-portkey-config": json.dumps(config)}
        headers.update({h: request.headers[h] for h in PASSTHROUGH_HEADERS if h in request.headers})

        response = None
        for url in url_pool.order():  # failover across gateway instances (GW-10)
            upstream = client.build_request("POST", f"{url}/chat/completions", json=upstream_body, headers=headers)
            try:
                response = await client.send(upstream, stream=True)
                break
            except (httpx2.ConnectError, httpx2.ConnectTimeout) as exc:
                # The request never reached this instance, so trying the next one can't duplicate anything.
                url_pool.mark_down(url)
                logger.error("Portkey gateway unreachable at %s: %s", url, type(exc).__name__)
            except httpx2.HTTPError as exc:
                # It may have reached the gateway (e.g. a read timeout): don't retry elsewhere.
                logger.error("Portkey gateway failed mid-request at %s: %s", url, type(exc).__name__)
                return _error(502, "gateway_error", "gateway_unreachable", "The LLM gateway did not complete the request.")
        if response is None:  # never fall back to a provider directly
            return _error(502, "gateway_error", "gateway_unreachable", "The LLM gateway is unreachable.")

        span.set_attribute("ent.router.upstream_status", response.status_code)
        if response.status_code == 446:  # a gateway guardrail denied the request or the answer
            content = await response.aread()
            await response.aclose()
            failed, from_answer = _failed_hooks(content)
            span.set_attribute("ent.guardrail.blocked", ",".join(failed) or "policy")
            logger.warning("model=%s client=%s blocked by gateway guardrail %s", name, label, failed)
            return _error(502 if from_answer else 400, "invalid_request_error", "guardrail_blocked",
                          f"Blocked by gateway guardrail: {', '.join(failed) or 'policy'}.")

        if wants_stream and mode != "record":
            logger.info("model=%s mode=%s status=%s stream=True client=%s", name, mode, response.status_code, label)
            return StreamingResponse(_relay(response), status_code=response.status_code,
                                     media_type=response.headers.get("content-type", "application/json"),
                                     background=BackgroundTask(response.aclose))

        content = await response.aread()  # whole answers: needed to count tokens, record and cache
        await response.aclose()
        logger.info("model=%s mode=%s status=%s client=%s", name, mode, response.status_code, label)
        if response.status_code != 200:
            return Response(content, status_code=response.status_code,
                            media_type=response.headers.get("content-type", "application/json"))
        try:
            data = json.loads(content)
        except ValueError:
            return Response(content, status_code=200, media_type=response.headers.get("content-type", "application/json"))
        data.pop("hook_results", None)  # Portkey echoes guardrail results, including an excerpt of the prompt: keep it out of replies, caches and recordings
        _note_answering_model(span, data)
        usage = data.get("usage") or {}
        total = usage.get("total_tokens") or (usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0))
        budgets.add(label, int(total or 0))
        if usage.get("prompt_tokens") is not None:
            span.set_attribute(obs.INPUT_TOKENS, int(usage["prompt_tokens"]))
            span.set_attribute(obs.OUTPUT_TOKENS, int(usage.get("completion_tokens", 0)))
        if mode == "record":
            path = recordings.save(request_key(body), name, data)
            logger.info("Recorded %s", path.name)
            return _completion(data, wants_stream)
        if cacheable:
            response_cache.put(cache_key, data, float(cache_settings.get("ttl_seconds", 300)))
            span.set_attribute("ent.router.cache", "miss")
            return JSONResponse(data, headers={"x-router-cache": "miss"})
        return JSONResponse(data)

    @api.post("/v1/chat/completions")
    async def chat_completions(request: Request) -> Response:
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return _error(400, "invalid_request_error", "invalid_json", "Request body must be valid JSON.")
        name = body.get("model") if isinstance(body, dict) else None
        label = keys.authenticate(request.headers.get("authorization"))
        if label is None:
            return _unauthorized()
        if name not in registry.models:
            return _error(
                400, "invalid_request_error", "model_not_approved",
                f"Model '{name}' is not approved. Approved models: {', '.join(registry.names())}.",
            )
        if "max_tokens" in body and "max_completion_tokens" not in body:
            # OpenAI's newer models reject `max_tokens` (they need `max_completion_tokens`); Portkey accepts the new name
            # for Anthropic too (verified live), so one name works for every target, including fallbacks.
            body = {**{k: v for k, v in body.items() if k != "max_tokens"}, "max_completion_tokens": body["max_tokens"]}
        default_limit = registry.models[name].get("default_max_completion_tokens")
        if default_limit and "max_completion_tokens" not in body:
            # Anthropic requires a token limit and Portkey does not add one, so without this a plain request would fail on
            # Claude and silently fall through to the next provider.
            body = {**body, "max_completion_tokens": default_limit}
        started = time.monotonic()
        with tracer.start_as_current_span(
            f"router chat {name}", context=propagate.extract(request.headers), kind=SpanKind.SERVER,
            attributes={obs.OPERATION: "chat", obs.REQUEST_MODEL: name, "ent.router.mode": mode, "ent.client": label,
                        obs.SPAN_TYPE: "chain"},
        ) as span:
            try:  # who is calling, from the metadata the SDK sends (names only)
                meta = json.loads(request.headers.get("x-portkey-metadata", "{}"))
                for key, attribute in (("agent", obs.AGENT_NAME), ("team", obs.TEAM)):
                    if isinstance(meta.get(key), str):
                        span.set_attribute(attribute, meta[key])
            except ValueError:
                pass
            response = await handle(body, name, label, request, span)
            span.set_attribute("http.response.status_code", response.status_code)
            span.set_attribute("ent.router.duration_ms", int((time.monotonic() - started) * 1000))
            if response.status_code >= 500:
                span.set_status(Status(StatusCode.ERROR, str(response.status_code)))
            return response

    return api


def _failed_hooks(content: bytes) -> tuple[list[str], bool]:
    """IDs of the guardrail hooks that failed in a 446 response, and whether any was an after-request (answer) hook."""
    try:
        results = json.loads(content).get("hook_results") or {}
    except (ValueError, AttributeError):
        return [], False
    failed: list[str] = []
    from_answer = False
    for stage in ("before_request_hooks", "after_request_hooks"):
        for hook in results.get(stage) or []:
            if isinstance(hook, dict) and hook.get("verdict") is False and isinstance(hook.get("id"), str):
                failed.append(hook["id"])
                from_answer = from_answer or stage == "after_request_hooks"
    return failed, from_answer


async def _relay(response: httpx2.Response) -> AsyncIterator[bytes]:
    async for chunk in response.aiter_raw():
        yield chunk


def _note_answering_model(span, data: Any) -> None:
    """Record which model actually answered on the span (`gen_ai.response.model`).

    The request names a logical model ("chat-default"); the answer says which provider model served it, so a
    fallback to another provider shows up in the trace.
    """
    model = data.get("model") if isinstance(data, dict) else None
    if isinstance(model, str) and model:
        span.set_attribute(obs.RESPONSE_MODEL, model)


def _completion(data: dict[str, Any], stream: bool, headers: dict[str, str] | None = None) -> Response:
    if stream:
        return StreamingResponse(_sse(data), media_type="text/event-stream", headers=headers)
    return JSONResponse(data, headers=headers)


def _sse(data: dict[str, Any]) -> Iterator[str]:
    """Turn a whole chat completion into the streaming wire format."""
    choice = data["choices"][0]
    message = choice.get("message", {})
    base = {"id": data.get("id", "replay"), "object": "chat.completion.chunk",
            "created": data.get("created", int(time.time())), "model": data.get("model", "")}
    delta: dict[str, Any] = {"role": "assistant"}
    if message.get("content"):
        delta["content"] = message["content"]
    if message.get("tool_calls"):
        delta["tool_calls"] = [{"index": i, **call} for i, call in enumerate(message["tool_calls"])]
    yield f"data: {json.dumps({**base, 'choices': [{'index': 0, 'delta': delta, 'finish_reason': None}]})}\n\n"
    last = {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": choice.get("finish_reason", "stop")}]}
    if data.get("usage"):
        last["usage"] = data["usage"]
    yield f"data: {json.dumps(last)}\n\n"
    yield "data: [DONE]\n\n"


def _stub(model: str, key: str) -> dict[str, Any]:
    """A deterministic, clearly marked answer for requests that have no recording."""
    text = f"[replay stub] No recording for this request (key {key[:12]}). Record one with ROUTER_MODE=record."
    return {
        "id": f"replay-{key[:12]}", "object": "chat.completion", "created": 0, "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def _error(status: int, error_type: str, code: str, message: str, headers: dict[str, str] | None = None) -> JSONResponse:
    """OpenAI-style error body, so OpenAI-compatible clients surface the message clearly."""
    return JSONResponse({"error": {"type": error_type, "code": code, "message": message}}, status_code=status, headers=headers)


def _unauthorized() -> JSONResponse:
    return _error(401, "authentication_error", "invalid_api_key",
                  "Missing or invalid gateway credential. Agents get one per team/agent; provider keys are never used here.")
