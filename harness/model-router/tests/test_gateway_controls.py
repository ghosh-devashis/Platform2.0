"""Tests for the gateway controls: auth (GW-02), guardrails (GW-05), limits (GW-07), telemetry (GW-08),
cache (GW-09), failover (GW-10)."""

import json

import httpx2
import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from model_router.app import DEFAULT_REGISTRY, Registry, create_app
from model_router.controls import GatewayKeys, RateLimiter, ResponseCache, TokenBudgets, UrlPool, parse_pairs

PROFILES = {
    "_comment": "ignored",
    "standard": {"before_request_hooks": [{"type": "guardrail", "id": "in-std"}], "after_request_hooks": [{"type": "guardrail", "id": "out-std"}]},
    "strict": {"before_request_hooks": [{"type": "guardrail", "id": "in-strict"}]},
}
REGISTRY = Registry(
    {
        "chat-default": {"targets": [{"provider": "anthropic", "model": "m", "key_secret": "llm/anthropic"}]},
        "chat-fast": {"targets": [{"provider": "anthropic", "model": "h", "key_secret": "llm/anthropic"}],
                      "cache": {"ttl_seconds": 60, "classifications": ["public", "internal"]}},
    },
    PROFILES,
)
BODY = {"model": "chat-default", "messages": [{"role": "user", "content": "hi"}]}
ANSWER = {"id": "1", "object": "chat.completion", "model": "m",
          "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
          "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}


class FakeSecrets:
    def get(self, name, **_):
        return "sk-test"


async def _chunks(data):
    yield data if isinstance(data, bytes) else json.dumps(data).encode()


def reply(status=200, data=ANSWER, content_type="application/json"):
    return httpx2.Response(status, headers={"content-type": content_type}, content=_chunks(data))


def router(handler=None, **kwargs):
    seen = []

    def capture(request):
        seen.append(request)
        return (handler or (lambda r: reply()))(request)

    kwargs.setdefault("portkey_url", "http://portkey.test/v1")
    app = create_app(registry=REGISTRY, secrets=FakeSecrets(), mode="real",
                     http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(capture)), **kwargs)
    return TestClient(app), seen


def chat(client, body=None, headers=None):
    return client.post("/v1/chat/completions", json=body or BODY, headers=headers or {})


def config_of(request):
    return json.loads(request.headers["x-portkey-config"])


# --- pure controls ------------------------------------------------------------------------------------------

def test_parse_pairs():
    assert parse_pairs("a=1, b = two ", "x") == {"a": "1", "b": "two"}
    assert parse_pairs("", "x") == {}
    with pytest.raises(ValueError):
        parse_pairs("novalue", "x")


def test_gateway_keys():
    open_gateway = GatewayKeys(None)
    assert open_gateway.enabled is False and open_gateway.authenticate(None) == "anonymous"
    keys = GatewayKeys({"chat-agent": "key-a", "tests": "key-b"})
    assert keys.authenticate("Bearer key-a") == "chat-agent" and keys.authenticate("Bearer key-b") == "tests"
    assert keys.authenticate("Bearer nope") is None and keys.authenticate(None) is None and keys.authenticate("") is None


def test_rate_limiter_sliding_window():
    now = [0.0]
    limiter = RateLimiter(2, clock=lambda: now[0])
    assert limiter.check("a") is None and limiter.check("a") is None
    wait = limiter.check("a")
    assert wait is not None and 1 <= wait <= 61
    assert limiter.check("b") is None  # separate client
    now[0] = 61
    assert limiter.check("a") is None  # window passed
    assert RateLimiter(0).check("a") is None  # 0 = off


def test_token_budgets_reset_each_day():
    day = ["2026-10-06"]
    budgets = TokenBudgets(100, today=lambda: day[0])
    assert not budgets.exceeded("a")
    budgets.add("a", 60)
    budgets.add("a", 40)
    assert budgets.exceeded("a") and not budgets.exceeded("b") and budgets.used("a") == 100
    day[0] = "2026-10-07"
    assert not budgets.exceeded("a")


def test_response_cache_ttl_and_size():
    now = [0.0]
    cache = ResponseCache(max_entries=2, clock=lambda: now[0])
    cache.put("a", 1, 10)
    cache.put("b", 2, 10)
    cache.put("c", 3, 10)  # evicts the oldest
    assert cache.get("a") is None and cache.get("b") == 2 and cache.get("c") == 3
    now[0] = 11
    assert cache.get("b") is None


def test_url_pool_round_robin_and_cooldown():
    now = [0.0]
    pool = UrlPool(["http://a/v1", "http://b/v1/"], cooldown_seconds=10, clock=lambda: now[0])
    assert pool.order()[0] == "http://a/v1" and pool.order()[0] == "http://b/v1"  # rotates, trailing slash trimmed
    pool.mark_down("http://a/v1")
    assert pool.order()[0] == "http://b/v1" and pool.order()[-1] == "http://a/v1"  # downed one is a last resort
    now[0] = 11
    assert set(pool.order()) == {"http://a/v1", "http://b/v1"}
    with pytest.raises(ValueError):
        UrlPool([])


# --- GW-02 authentication -----------------------------------------------------------------------------------

def test_agents_need_their_gateway_credential():
    client, seen = router(gateway_keys={"chat-agent": "dev-key-1"})
    assert chat(client).status_code == 401
    assert chat(client, headers={"authorization": "Bearer wrong"}).json()["error"]["code"] == "invalid_api_key"
    assert client.get("/v1/models").status_code == 401
    assert seen == []  # nothing reached the gateway
    ok = chat(client, headers={"authorization": "Bearer dev-key-1"})
    assert ok.status_code == 200
    assert client.get("/v1/models", headers={"authorization": "Bearer dev-key-1"}).status_code == 200
    assert "authorization" not in seen[0].headers  # the agent's credential is never forwarded to the gateway
    assert client.get("/health").json()["auth"] is True


def test_open_when_no_keys_configured():
    client, _ = router(gateway_keys={})
    assert chat(client).status_code == 200


# --- GW-05 gateway guardrails -------------------------------------------------------------------------------

def test_guardrail_profile_hooks_are_added_to_the_portkey_config():
    client, seen = router()
    chat(client, headers={"x-ent-guardrail-profile": "strict"})
    assert [h["id"] for h in config_of(seen[0])["before_request_hooks"]] == ["in-strict"]
    chat(client)  # no header: the standard profile
    config = config_of(seen[1])
    assert [h["id"] for h in config["before_request_hooks"]] == ["in-std"]
    assert [h["id"] for h in config["after_request_hooks"]] == ["out-std"]


def test_unknown_guardrail_profile_is_rejected():
    client, seen = router()
    for profile in ("lenient", "_comment"):
        response = chat(client, headers={"x-ent-guardrail-profile": profile})
        assert response.status_code == 400 and response.json()["error"]["code"] == "unknown_guardrail_profile"
    assert seen == []


def test_gateway_guardrail_denial_becomes_a_clear_error():
    before = {"hook_results": {"before_request_hooks": [{"id": "ent-input-no-pii", "verdict": False}]}}
    client, _ = router(lambda r: reply(446, before))
    response = chat(client)
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "guardrail_blocked" and "ent-input-no-pii" in error["message"]

    after = {"hook_results": {"after_request_hooks": [{"id": "ent-output-no-secrets", "verdict": False}]}}
    client, _ = router(lambda r: reply(446, after))
    assert chat(client).status_code == 502
    client, _ = router(lambda r: reply(446, b"not json"))
    assert chat(client).json()["error"]["code"] == "guardrail_blocked"


def test_shipped_registry_guardrail_profiles_cover_standard_and_strict():
    registry = Registry.load(DEFAULT_REGISTRY)
    assert set(registry.guardrail_profiles) == {"standard", "strict"}  # the _comment key is not a profile
    for hooks in registry.guardrail_profiles.values():
        for hook in hooks["before_request_hooks"] + hooks["after_request_hooks"]:
            assert hook["type"] == "guardrail" and hook["deny"] is True
            assert hook["checks"][0]["id"] == "default.regexMatch" and hook["checks"][0]["parameters"]["not"] is True
    import re
    secrets_rule = registry.guardrail_profiles["standard"]["before_request_hooks"][0]["checks"][0]["parameters"]["rule"]
    assert re.search(secrets_rule, "key AKIAABCDEFGHIJKLMNOP") and re.search(secrets_rule, "sk-abcdefghijklmnopqrstuvwxyz")
    assert not re.search(secrets_rule, "an ordinary sentence")
    pii_rule = registry.guardrail_profiles["strict"]["before_request_hooks"][1]["checks"][0]["parameters"]["rule"]
    assert re.search(pii_rule, "ssn 123-45-6789") and re.search(pii_rule, "mail bob@example.com")


# --- GW-07 limits -------------------------------------------------------------------------------------------

def test_rate_limit_per_client():
    client, seen = router(gateway_keys={"a": "ka", "b": "kb"}, rate_limit_rpm=2)
    for _ in range(2):
        assert chat(client, headers={"authorization": "Bearer ka"}).status_code == 200
    limited = chat(client, headers={"authorization": "Bearer ka"})
    assert limited.status_code == 429 and limited.json()["error"]["code"] == "rate_limit_exceeded"
    assert int(limited.headers["retry-after"]) >= 1
    assert chat(client, headers={"authorization": "Bearer kb"}).status_code == 200  # another client is unaffected
    assert len(seen) == 3


def test_daily_token_budget_per_client():
    client, seen = router(gateway_keys={"a": "ka"}, token_budget_per_day=20)
    headers = {"authorization": "Bearer ka"}
    assert chat(client, headers=headers).status_code == 200  # 15 tokens
    assert chat(client, headers=headers).status_code == 200  # still under 20 before this call (15 < 20): now 30
    blocked = chat(client, headers=headers)
    assert blocked.status_code == 429 and blocked.json()["error"]["code"] == "budget_exceeded"
    assert len(seen) == 2


# --- GW-09 cache --------------------------------------------------------------------------------------------

def test_cache_serves_identical_non_sensitive_requests_for_opted_in_models():
    client, seen = router()
    body = {**BODY, "model": "chat-fast"}
    internal = {"x-ent-data-classification": "internal"}
    first = chat(client, body, internal)
    second = chat(client, body, internal)
    assert first.headers["x-router-cache"] == "miss" and second.headers["x-router-cache"] == "hit"
    assert second.json() == ANSWER and len(seen) == 1
    # a different question is not served from the cache
    chat(client, {**body, "messages": [{"role": "user", "content": "other"}]}, internal)
    assert len(seen) == 2


def test_cache_is_not_used_for_sensitive_callers_streams_or_models_that_did_not_opt_in():
    client, seen = router()
    body = {**BODY, "model": "chat-fast"}
    for headers in ({"x-ent-data-classification": "restricted"}, {"x-ent-data-classification": "confidential"}, {}):
        chat(client, body, headers)
        chat(client, body, headers)
    assert len(seen) == 6
    client, seen = router()
    chat(client, BODY, {"x-ent-data-classification": "public"})
    chat(client, BODY, {"x-ent-data-classification": "public"})  # chat-default did not opt in
    assert len(seen) == 2


# --- GW-10 failover -----------------------------------------------------------------------------------------

def test_failover_to_the_next_gateway_instance():
    def handler(request):
        if request.url.host == "portkey-a.test":
            raise httpx2.ConnectError("refused")
        return reply()

    client, seen = router(handler, portkey_url=["http://portkey-a.test/v1", "http://portkey-b.test/v1"])
    for _ in range(3):
        assert chat(client).status_code == 200
    assert {r.url.host for r in seen} == {"portkey-a.test", "portkey-b.test"}
    assert [r.url.host for r in seen][-2:] == ["portkey-b.test", "portkey-b.test"]  # a is skipped while it is down


def test_all_gateways_down_is_a_clear_error_never_a_bypass():
    def down(request):
        raise httpx2.ConnectError("refused")

    client, _ = router(down, portkey_url=["http://a.test/v1", "http://b.test/v1"])
    response = chat(client)
    assert response.status_code == 502 and response.json()["error"]["code"] == "gateway_unreachable"
    assert client.get("/health").json()["gateways"] == 2


# --- GW-08 / GW-04 telemetry --------------------------------------------------------------------------------

def test_each_call_is_a_span_joined_to_the_agents_trace():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    client, _ = router(gateway_keys={"chat-agent": "k"}, tracer_provider=provider)
    trace_id, parent = "4bf92f3577b34da6a3ce929d0e0e4736", "00f067aa0ba902b7"
    chat(client, headers={
        "authorization": "Bearer k", "traceparent": f"00-{trace_id}-{parent}-01",
        "x-portkey-metadata": json.dumps({"agent": "chat-agent", "team": "platform"}),
        "x-ent-guardrail-profile": "strict",
    })
    (span,) = exporter.get_finished_spans()
    assert span.name == "router chat chat-default"
    assert format(span.context.trace_id, "032x") == trace_id and format(span.parent.span_id, "016x") == parent
    attributes = dict(span.attributes)
    assert attributes["ent.client"] == "chat-agent" and attributes["gen_ai.agent.name"] == "chat-agent"
    assert attributes["ent.team"] == "platform" and attributes["ent.guardrail.profile"] == "strict"
    assert attributes["gen_ai.usage.input_tokens"] == 10 and attributes["gen_ai.usage.output_tokens"] == 5
    assert attributes["http.response.status_code"] == 200 and attributes["ent.router.mode"] == "real"
    assert "hi" not in json.dumps(attributes)  # no prompt content
    assert attributes["gen_ai.request.model"] == "chat-default" and attributes["gen_ai.response.model"] == "m"


def test_the_span_records_which_model_answered_including_after_a_fallback_and_from_the_cache():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    fallback_answer = {**ANSWER, "model": "gpt-from-the-fallback"}  # another provider answered the same logical name
    client, _ = router(lambda r: reply(data=fallback_answer), tracer_provider=provider)
    body = {**BODY, "model": "chat-fast"}
    internal = {"x-ent-data-classification": "internal"}
    chat(client, body, internal)  # live answer
    chat(client, body, internal)  # same question: served from the cache
    live, cached = exporter.get_finished_spans()
    for span in (live, cached):
        attributes = dict(span.attributes)
        assert attributes["gen_ai.request.model"] == "chat-fast"  # what the agent asked for
        assert attributes["gen_ai.response.model"] == "gpt-from-the-fallback"  # what actually answered
    assert dict(live.attributes).get("ent.router.cache") == "miss" and dict(cached.attributes)["ent.router.cache"] == "hit"


def test_upstream_errors_mark_the_span():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    def down(request):
        raise httpx2.ConnectError("refused")

    client, _ = router(down, tracer_provider=provider)
    chat(client)
    (span,) = exporter.get_finished_spans()
    assert span.status.status_code.name == "ERROR" and span.attributes["http.response.status_code"] == 502

def test_failover_only_for_connection_failures_never_after_the_request_may_have_been_sent():
    def handler(request):
        if request.url.host == "portkey-a.test":
            raise httpx2.ReadTimeout("slow")  # the request may already have reached the gateway
        return reply()

    client, seen = router(handler, portkey_url=["http://portkey-a.test/v1", "http://portkey-b.test/v1"])
    response = chat(client)
    assert response.status_code == 502 and response.json()["error"]["code"] == "gateway_unreachable"
    assert [r.url.host for r in seen] == ["portkey-a.test"]  # b was never tried: no duplicate side effects

def test_max_tokens_is_sent_to_providers_as_max_completion_tokens():
    """OpenAI's newer models reject `max_tokens`; one name works for every target, fallbacks included."""
    client, seen = router()
    chat(client, {**BODY, "max_tokens": 50})
    sent = json.loads(seen[0].content)
    assert sent["max_completion_tokens"] == 50 and "max_tokens" not in sent
    chat(client, {**BODY, "max_completion_tokens": 7, "max_tokens": 50})  # an explicit new name wins
    assert json.loads(seen[1].content)["max_completion_tokens"] == 7


def test_cache_distinguishes_different_token_limits():
    client, seen = router()
    body = {**BODY, "model": "chat-fast"}
    internal = {"x-ent-data-classification": "internal"}
    chat(client, {**body, "max_tokens": 5}, internal)
    chat(client, {**body, "max_tokens": 500}, internal)  # a shorter answer must not be served for a longer allowance
    assert len(seen) == 2

def test_a_default_token_limit_is_added_only_when_the_caller_sets_none():
    """Anthropic requires max_tokens; without a default a plain request fails on Claude and silently falls back."""
    registry = Registry({"chat-default": {"targets": [{"provider": "anthropic", "model": "m", "key_secret": "k"}],
                                          "default_max_completion_tokens": 4096}})
    seen = []

    def capture(request):
        seen.append(request)
        return reply()

    client = TestClient(create_app(registry=registry, secrets=FakeSecrets(), mode="real", portkey_url="http://portkey.test/v1",
                                   http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(capture))))
    chat(client)
    assert json.loads(seen[0].content)["max_completion_tokens"] == 4096
    chat(client, {**BODY, "max_tokens": 10})
    assert json.loads(seen[1].content)["max_completion_tokens"] == 10
    chat(client, {**BODY, "max_completion_tokens": 77})
    assert json.loads(seen[2].content)["max_completion_tokens"] == 77


def test_target_params_are_sent_to_the_provider_and_the_shipped_registry_uses_them():
    registry = Registry({"m": {"targets": [{"provider": "openai", "model": "gpt-x", "key_secret": "k",
                                            "params": {"reasoning_effort": "none"}}]}})
    config = registry.portkey_config("m", FakeSecrets())
    assert config["override_params"] == {"model": "gpt-x", "reasoning_effort": "none"}
    shipped = Registry.load(DEFAULT_REGISTRY)
    openai_target = next(t for t in shipped.models["chat-default"]["targets"] if t["provider"] == "openai")
    assert openai_target["params"] == {"reasoning_effort": "none"}  # gpt-5.6 refuses function tools on the chat API otherwise
    assert all(entry.get("default_max_completion_tokens", 0) > 0 for entry in shipped.models.values())
