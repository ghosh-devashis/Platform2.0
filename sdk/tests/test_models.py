"""Tests for get_model() (SDK-07, GW-01, GW-04, SDK-16). The gateway is stubbed with a mock HTTP transport."""

import json

import httpx2
import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, MessagesState, StateGraph
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from ent_agent_sdk import EnterpriseAgentApp
from ent_agent_sdk import observability as obs
from ent_agent_sdk.app import TRACE_ID_HEADER
from ent_agent_sdk.models import ModelConfigError, get_model

COMPLETION = {
    "id": "c1", "object": "chat.completion", "created": 0, "model": "claude-opus-5-5",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "Hello!"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 11, "completion_tokens": 4, "total_tokens": 15},
}


def stubbed_model(name="chat-default"):
    """A get_model() model whose HTTP calls go to a stub gateway. Returns (model, captured requests)."""
    seen = []

    def gateway(request):
        seen.append(request)
        return httpx2.Response(200, json=COMPLETION)

    model = get_model(name)
    model.root_client._client._transport = httpx2.MockTransport(gateway)
    model.root_async_client._client._transport = httpx2.MockTransport(gateway)
    return model, seen


def test_model_points_at_the_gateway_without_sdk_retries(monkeypatch):
    monkeypatch.setenv("ENT_MODEL_GATEWAY_URL", "http://gateway.test/v1")
    model = get_model("chat-default", temperature=0)
    assert model.model_name == "chat-default"
    assert model.openai_api_base == "http://gateway.test/v1"
    assert model.max_retries == 0
    assert model.temperature == 0


@pytest.mark.parametrize("override", [{"base_url": "https://api.openai.com/v1"}, {"api_key": "sk-x"}, {"max_retries": 3}])
def test_gateway_settings_cannot_be_overridden(override):
    with pytest.raises(ModelConfigError):
        get_model("chat-default", **override)


def test_request_uses_logical_name_and_no_correlation_outside_a_trace():
    model, seen = stubbed_model()
    assert model.invoke([HumanMessage(content="hi")]).content == "Hello!"
    assert json.loads(seen[0].content)["model"] == "chat-default"
    assert "x-portkey-trace-id" not in seen[0].headers


@pytest.mark.parametrize("use_async", [False, True])
def test_llm_call_inside_agent_is_traced_and_correlated(use_async):
    model, seen = stubbed_model()

    def respond(state: MessagesState, config: RunnableConfig) -> dict:
        return {"messages": [model.invoke(state["messages"], config)]}

    async def arespond(state: MessagesState, config: RunnableConfig) -> dict:
        return {"messages": [await model.ainvoke(state["messages"], config)]}

    node = arespond if use_async else respond
    builder = StateGraph(MessagesState)
    builder.add_node("respond", node)
    builder.add_edge(START, "respond")
    builder.add_edge("respond", END)

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    app = EnterpriseAgentApp(builder.compile(), name="test-agent", version="1.2.3", team="payments",
                             tracer_provider=provider)
    response = TestClient(app.asgi).post("/invocations", json={"prompt": "hi"})
    assert response.json()["output"] == "Hello!"
    trace_id = response.headers[TRACE_ID_HEADER]

    # GW-04: the gateway request joins the agent's trace.
    headers = seen[0].headers
    assert headers["x-portkey-trace-id"] == trace_id
    assert json.loads(headers["x-portkey-metadata"]) == {"agent": "test-agent", "agent_version": "1.2.3",
                                                         "team": "payments"}
    assert trace_id in headers["traceparent"]

    # SDK-03: LLM span with logical and actual model, and token usage.
    llm = next(s for s in exporter.get_finished_spans() if s.name.startswith("chat "))
    assert llm.name == "chat chat-default"
    assert llm.attributes[obs.REQUEST_MODEL] == "chat-default"
    assert llm.attributes[obs.RESPONSE_MODEL] == "claude-opus-5-5"
    assert llm.attributes[obs.INPUT_TOKENS] == 11
    assert llm.attributes[obs.OUTPUT_TOKENS] == 4
