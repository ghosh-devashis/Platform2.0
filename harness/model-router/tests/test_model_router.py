"""Tests for the model router (SDK-07, GW-02, GW-03, GW-04, GW-10). Portkey and Secrets Manager are stubbed."""

import json

import httpx2
import pytest
from fastapi.testclient import TestClient

from ent_agent_sdk.secrets import SecretNotFoundError
from model_router.app import DEFAULT_REGISTRY, Registry, create_app

REGISTRY = Registry({
    "chat-default": {
        "strategy": "fallback",
        "targets": [
            {"provider": "anthropic", "model": "claude-opus-5-5", "key_secret": "llm/anthropic"},
            {"provider": "openai", "model": "gpt-x", "key_secret": "llm/openai"},
        ],
        "retry": {"attempts": 2, "on_status_codes": [429]},
        "request_timeout_ms": 1000,
    },
    "chat-fast": {"targets": [{"provider": "anthropic", "model": "claude-haiku-4-5", "key_secret": "llm/anthropic"}]},
})
COMPLETION = {"id": "1", "object": "chat.completion", "model": "claude-opus-5-5",
              "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}]}


class FakeSecrets:
    def __init__(self, missing=()):
        self.missing = set(missing)

    def get(self, name, **_):
        if name in self.missing:
            raise SecretNotFoundError(f"Secret '{name}' was not found.")
        return f"sk-test-{name.split('/')[-1]}"


def router(handler, secrets=None):
    """A router whose Portkey upstream is `handler`. Returns (client, captured requests)."""
    seen = []

    def capture(request):
        seen.append(request)
        return handler(request)

    app = create_app(registry=REGISTRY, portkey_url="http://portkey.test/v1", secrets=secrets or FakeSecrets(),
                     http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(capture)))
    return TestClient(app), seen


def streamed(status, body, content_type="application/json"):
    """A streamed upstream response, like a real network response (fixed-bytes responses are pre-read)."""
    async def chunks():
        yield body if isinstance(body, bytes) else json.dumps(body).encode()

    return httpx2.Response(status, headers={"content-type": content_type}, content=chunks())


def ok(request):
    return streamed(200, COMPLETION)


def chat(client, model="chat-default", headers=None, **extra):
    return client.post("/v1/chat/completions", headers=headers or {},
                       json={"model": model, "messages": [{"role": "user", "content": "hi"}], **extra})


def test_lists_approved_models():
    client, _ = router(ok)
    assert [m["id"] for m in client.get("/v1/models").json()["data"]] == ["chat-default", "chat-fast"]


def test_unapproved_model_is_rejected_with_the_approved_list():
    client, seen = router(ok)
    response = chat(client, model="gpt-anything")
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "model_not_approved"
    assert "chat-default, chat-fast" in error["message"]
    assert seen == []  # nothing reached the gateway


def test_fallback_model_becomes_a_portkey_config_with_keys():
    client, seen = router(ok)
    response = chat(client)
    assert response.status_code == 200
    assert response.json() == COMPLETION

    config = json.loads(seen[0].headers["x-portkey-config"])
    assert config["strategy"] == {"mode": "fallback"}
    assert config["targets"] == [
        {"provider": "anthropic", "api_key": "sk-test-anthropic", "override_params": {"model": "claude-opus-5-5"}},
        {"provider": "openai", "api_key": "sk-test-openai", "override_params": {"model": "gpt-x"}},
    ]
    assert config["retry"] == {"attempts": 2, "on_status_codes": [429]}
    assert config["request_timeout"] == 1000
    assert str(seen[0].url) == "http://portkey.test/v1/chat/completions"


def test_single_target_config_is_flat():
    client, seen = router(ok)
    chat(client, model="chat-fast")
    config = json.loads(seen[0].headers["x-portkey-config"])
    assert config["provider"] == "anthropic"
    assert config["override_params"] == {"model": "claude-haiku-4-5"}
    assert "targets" not in config


def test_correlation_headers_pass_through_but_agent_credential_does_not():
    client, seen = router(ok)
    chat(client, headers={"x-portkey-trace-id": "abc", "x-portkey-metadata": '{"agent":"a"}',
                          "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
                          "authorization": "Bearer agent-gateway-key"})
    headers = seen[0].headers
    assert headers["x-portkey-trace-id"] == "abc"
    assert headers["x-portkey-metadata"] == '{"agent":"a"}'
    assert headers["traceparent"].startswith("00-4bf92f")
    assert "authorization" not in headers


def test_missing_provider_key_fails_closed():
    client, seen = router(ok, secrets=FakeSecrets(missing={"llm/openai"}))
    response = chat(client)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "provider_credentials_unavailable"
    assert seen == []


def test_gateway_down_is_a_clear_error_not_a_bypass():
    def down(request):
        raise httpx2.ConnectError("refused")

    client, _ = router(down)
    response = chat(client)
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "gateway_unreachable"
    assert "sk-test" not in response.text


def test_gateway_errors_and_streams_are_relayed():
    def stream(request):
        return streamed(200, b'data: {"choices":[]}\n\ndata: [DONE]\n\n', "text/event-stream")

    client, _ = router(stream)
    response = chat(client, stream=True)
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.text.endswith("data: [DONE]\n\n")

    client, _ = router(lambda r: streamed(429, {"error": {"message": "rate limited"}}))
    assert chat(client).status_code == 429


def test_shipped_registry_is_valid():
    registry = Registry.load(DEFAULT_REGISTRY)
    assert {"chat-default", "chat-fast"} <= set(registry.names())
    for name, entry in registry.models.items():
        assert entry["targets"], name
        for target in entry["targets"]:
            assert {"provider", "model", "key_secret"} <= set(target), name
            assert "TBD" not in target["model"], f"{name}: model ID not filled in"
