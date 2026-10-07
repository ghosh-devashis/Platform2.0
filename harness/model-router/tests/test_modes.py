"""Tests for the router's replay, record and local modes (RUN-06)."""

import json

import httpx2
import pytest
from fastapi.testclient import TestClient

from model_router.app import Registry, create_app
from model_router.recordings import RecordingStore, request_key

REGISTRY = Registry({
    "chat-default": {
        "targets": [{"provider": "anthropic", "model": "claude-opus-5-5", "key_secret": "llm/anthropic"}],
        "local": {"model": "llama3.1", "custom_host": "http://ollama.test:11434"},
    },
    "chat-fast": {"targets": [{"provider": "anthropic", "model": "claude-haiku-4-5", "key_secret": "llm/anthropic"}]},
})
BODY = {"model": "chat-default", "messages": [{"role": "user", "content": "hi"}]}
ANSWER = {"id": "x1", "object": "chat.completion", "created": 1, "model": "claude-opus-5-5",
          "choices": [{"index": 0, "message": {"role": "assistant", "content": "Hello!"}, "finish_reason": "stop"}],
          "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}}
TOOL_ANSWER = {"id": "x2", "object": "chat.completion", "created": 1, "model": "m",
               "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                   "role": "assistant", "content": None,
                   "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "t", "arguments": "{}"}}]}}]}


class FakeSecrets:
    def get(self, name, **_):
        return "sk-test"


async def _chunks(data):
    yield json.dumps(data).encode()


def router(tmp_path, mode, upstream=None, **kwargs):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx2.Response(200, headers={"content-type": "application/json"}, content=_chunks(ANSWER))

    app = create_app(registry=REGISTRY, portkey_url="http://portkey.test/v1", secrets=FakeSecrets(), mode=mode,
                     recordings=RecordingStore(tmp_path),
                     http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(upstream or handler)), **kwargs)
    return TestClient(app), seen


def test_request_key_ignores_sampling_but_not_content():
    base = request_key(BODY)
    assert request_key({**BODY, "temperature": 0.9, "max_tokens": 5}) == base
    assert request_key({**BODY, "messages": [{"role": "user", "content": "bye"}]}) != base
    assert request_key({**BODY, "tools": [{"type": "function"}]}) != base


def test_replay_serves_recording_without_gateway_or_keys(tmp_path):
    RecordingStore(tmp_path).save(request_key(BODY), "chat-default", ANSWER)
    client, seen = router(tmp_path, "replay")
    response = client.post("/v1/chat/completions", json=BODY)
    assert response.status_code == 200
    assert response.json() == ANSWER
    assert response.headers["x-router-replay"] == "hit"
    assert seen == []


def test_replay_streams_recorded_answers_including_tool_calls(tmp_path):
    body = {**BODY, "stream": True}
    RecordingStore(tmp_path).save(request_key(body), "chat-default", TOOL_ANSWER)
    client, _ = router(tmp_path, "replay")
    text = client.post("/v1/chat/completions", json=body).text
    events = [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: {")]
    assert events[0]["choices"][0]["delta"]["tool_calls"][0]["function"]["name"] == "t"
    assert events[-1]["choices"][0]["finish_reason"] == "tool_calls"
    assert text.rstrip().endswith("data: [DONE]")


def test_replay_missing_recording_stub_or_error(tmp_path):
    client, _ = router(tmp_path, "replay", replay_missing="stub")
    stub = client.post("/v1/chat/completions", json=BODY)
    assert stub.status_code == 200
    assert stub.headers["x-router-replay"] == "stub"
    assert "[replay stub]" in stub.json()["choices"][0]["message"]["content"]

    client, _ = router(tmp_path, "replay", replay_missing="error")
    missing = client.post("/v1/chat/completions", json=BODY)
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "recording_not_found"


def test_record_saves_answer_and_replay_then_serves_it(tmp_path):
    client, seen = router(tmp_path, "record")
    assert client.post("/v1/chat/completions", json=BODY).json() == ANSWER
    assert len(seen) == 1
    assert RecordingStore(tmp_path).load(request_key(BODY)) == ANSWER

    replay_client, replay_seen = router(tmp_path, "replay", replay_missing="error")
    assert replay_client.post("/v1/chat/completions", json=BODY).json() == ANSWER
    assert replay_seen == []


def test_record_with_streaming_request_records_whole_answer(tmp_path):
    body = {**BODY, "stream": True}
    client, seen = router(tmp_path, "record")
    text = client.post("/v1/chat/completions", json=body).text
    assert "Hello!" in text and text.rstrip().endswith("data: [DONE]")
    assert "stream" not in json.loads(seen[0].content)  # upstream asked for a whole answer
    assert RecordingStore(tmp_path).load(request_key(body)) == ANSWER


def test_record_does_not_save_failures(tmp_path):
    def failing(request):
        return httpx2.Response(401, headers={"content-type": "application/json"}, content=_chunks({"error": "no"}))

    client, _ = router(tmp_path, "record", upstream=failing)
    assert client.post("/v1/chat/completions", json=BODY).status_code == 401
    assert RecordingStore(tmp_path).load(request_key(BODY)) is None


def test_local_mode_routes_to_ollama_without_provider_keys(tmp_path):
    client, seen = router(tmp_path, "local")
    assert client.post("/v1/chat/completions", json=BODY).status_code == 200
    config = json.loads(seen[0].headers["x-portkey-config"])
    assert config["provider"] == "ollama"
    assert config["custom_host"] == "http://ollama.test:11434"
    assert config["override_params"] == {"model": "llama3.1"}
    assert "api_key" not in config

    missing = client.post("/v1/chat/completions", json={**BODY, "model": "chat-fast"})
    assert missing.status_code == 503
    assert missing.json()["error"]["code"] == "local_model_not_configured"


def test_invalid_mode_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        router(tmp_path, "teleport")

def test_portkeys_guardrail_echo_never_reaches_replies_or_recordings(tmp_path):
    """Portkey adds `hook_results` with an excerpt of the prompt; recordings hold model answers only."""
    echoed = {**ANSWER, "hook_results": {"before_request_hooks": [{"id": "h", "checks": [{"data": {"textExcerpt": "my private prompt"}}]}]}}

    def upstream(request):
        return httpx2.Response(200, headers={"content-type": "application/json"}, content=_chunks(echoed))

    client, _ = router(tmp_path, "record", upstream=upstream)
    reply = client.post("/v1/chat/completions", json=BODY)
    assert "hook_results" not in reply.json() and "my private prompt" not in reply.text
    saved = RecordingStore(tmp_path).load(request_key(BODY))
    assert saved == ANSWER and "my private prompt" not in json.dumps(saved)

    live_client, _ = router(tmp_path, "real", upstream=upstream)  # normal (non-record) path too
    assert "hook_results" not in live_client.post("/v1/chat/completions", json=BODY).json()
