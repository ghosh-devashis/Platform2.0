"""Tests for tracing, JSON logs and health (SDK-03, SDK-04, SDK-16)."""

import json
import logging
import socket
import time

import httpx
import pytest
from fastapi.testclient import TestClient
from langchain_core.language_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from langgraph.graph import END, START, MessagesState, StateGraph
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from ent_agent_sdk import EnterpriseAgentApp
from ent_agent_sdk import observability as obs
from ent_agent_sdk.app import SESSION_HEADER, TRACE_ID_HEADER


def _graph(*nodes):
    builder = StateGraph(MessagesState)
    previous = START
    for node in nodes:
        builder.add_node(node.__name__, node)
        builder.add_edge(previous, node.__name__)
        previous = node.__name__
    builder.add_edge(previous, END)
    return builder.compile()


def _traced(*nodes, **kwargs):
    """An app whose spans are captured in memory. Returns (client, exporter)."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    app = EnterpriseAgentApp(_graph(*nodes), name="test-agent", version="1.2.3", tracer_provider=provider, **kwargs)
    return TestClient(app.asgi), exporter


def _by_name(exporter):
    return {span.name: span for span in exporter.get_finished_spans()}


def answer(state: MessagesState) -> dict:
    return {"messages": [AIMessage(content="ok")]}


def fail(state: MessagesState) -> dict:
    raise RuntimeError("secret detail that must not leak")


@tool
def lookup(query: str) -> str:
    """Look something up."""
    return f"result for {query}"


def use_tool(state: MessagesState, config: RunnableConfig) -> dict:
    return {"messages": [AIMessage(content=lookup.invoke({"query": "x"}, config))]}


def call_model(state: MessagesState, config: RunnableConfig) -> dict:
    reply = AIMessage(content="hi", usage_metadata={"input_tokens": 7, "output_tokens": 3, "total_tokens": 10})
    model = GenericFakeChatModel(messages=iter([reply]))
    return {"messages": [model.invoke(state["messages"], config)]}


def test_invocation_produces_server_span_with_standard_attributes():
    client, exporter = _traced(answer, team="payments")
    response = client.post("/invocations", json={"prompt": "hi"}, headers={SESSION_HEADER: "sess-1"})
    assert response.status_code == 200

    spans = _by_name(exporter)
    server = spans["invoke_agent test-agent"]
    assert server.attributes[obs.AGENT_NAME] == "test-agent"
    assert server.attributes[obs.AGENT_VERSION] == "1.2.3"
    assert server.attributes[obs.TEAM] == "payments"
    assert server.attributes[obs.CONVERSATION_ID] == "sess-1"
    assert server.attributes[obs.OUTCOME] == "success"
    assert server.attributes[obs.SPAN_TYPE] == "agent"
    assert response.headers[TRACE_ID_HEADER] == format(server.context.trace_id, "032x")


def test_node_span_is_child_of_server_span_and_internals_are_hidden():
    client, exporter = _traced(answer)
    client.post("/invocations", json={"prompt": "hi"})

    spans = _by_name(exporter)
    assert set(spans) == {"invoke_agent test-agent", "answer"}
    node, server = spans["answer"], spans["invoke_agent test-agent"]
    assert node.parent.span_id == server.context.span_id
    assert node.attributes[obs.NODE] == "answer"
    assert node.attributes[obs.SPAN_TYPE] == "chain"


def test_llm_span_records_model_and_tokens_but_no_content():
    client, exporter = _traced(call_model)
    client.post("/invocations", json={"prompt": "tell me a secret"})

    llm = next(s for s in exporter.get_finished_spans() if s.name.startswith("chat "))
    assert llm.attributes[obs.OPERATION] == "chat"
    assert llm.attributes[obs.SPAN_TYPE] == "llm"
    assert llm.attributes[obs.INPUT_TOKENS] == 7
    assert llm.attributes[obs.OUTPUT_TOKENS] == 3
    assert llm.parent.span_id == _by_name(exporter)["call_model"].context.span_id
    assert "tell me a secret" not in json.dumps(dict(llm.attributes))


def test_tool_span_records_tool_name():
    client, exporter = _traced(use_tool)
    client.post("/invocations", json={"prompt": "hi"})

    tool_span = _by_name(exporter)["execute_tool lookup"]
    assert tool_span.attributes[obs.TOOL_NAME] == "lookup"
    assert tool_span.attributes[obs.SPAN_TYPE] == "tool"
    assert tool_span.parent.span_id == _by_name(exporter)["use_tool"].context.span_id


def test_failure_marks_spans_as_errors_without_the_message():
    client, exporter = _traced(fail)
    response = client.post("/invocations", json={"prompt": "hi"})
    assert response.status_code == 500
    assert TRACE_ID_HEADER in response.headers

    spans = _by_name(exporter)
    server, node = spans["invoke_agent test-agent"], spans["fail"]
    for span in (server, node):
        assert span.status.status_code is StatusCode.ERROR
        assert span.attributes[obs.ERROR_TYPE] == "RuntimeError"
    assert server.attributes[obs.OUTCOME] == "error"
    assert all("secret detail" not in json.dumps(dict(s.attributes)) for s in spans.values())


def test_incoming_traceparent_is_continued():
    client, exporter = _traced(answer)
    trace_id, parent_id = "4bf92f3577b34da6a3ce929d0e0e4736", "00f067aa0ba902b7"
    client.post("/invocations", json={"prompt": "hi"}, headers={"traceparent": f"00-{trace_id}-{parent_id}-01"})

    server = _by_name(exporter)["invoke_agent test-agent"]
    assert format(server.context.trace_id, "032x") == trace_id
    assert format(server.parent.span_id, "016x") == parent_id


@pytest.mark.parametrize("check", [lambda: False, lambda: 1 / 0])
def test_ping_reports_unhealthy_when_health_check_fails(check):
    client, _ = _traced(answer, health_check=check)
    response = client.get("/ping")
    assert response.status_code == 503
    assert response.json()["status"] == "Unhealthy"


def test_ping_healthy_when_health_check_passes():
    client, _ = _traced(answer, health_check=lambda: True)
    assert client.get("/ping").json()["status"] == "Healthy"


def test_fastapi_builtin_telemetry_is_off():
    """FastAPI >=0.142 has its own OpenTelemetry; it must not add a second trace or export metrics."""
    client, _ = _traced(answer)
    config = client.app._telemetry
    assert not any(config[k] for k in ("tracing", "metrics", "logs", "operation_spans", "auto_configure"))


def test_json_logs_carry_trace_ids():
    provider = TracerProvider()
    record = logging.LogRecord("agent", logging.INFO, __file__, 1, "hello %s", ("world",), None)
    formatter = obs.JsonLogFormatter("test-agent")
    with provider.get_tracer("t").start_as_current_span("work") as span:
        entry = json.loads(formatter.format(record))
    assert entry["message"] == "hello world"
    assert entry["service"] == "test-agent"
    assert entry["trace_id"] == format(span.get_span_context().trace_id, "032x")
    assert "trace_id" not in json.loads(formatter.format(record))  # outside a span


def test_exporter_is_chosen_by_configuration(monkeypatch):
    def processors(provider):
        return provider._active_span_processor._span_processors

    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", raising=False)
    assert processors(obs.build_tracer_provider(service_name="a", service_version="1")) == ()

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
    configured = obs.build_tracer_provider(service_name="a", service_version="1", team="t")
    assert isinstance(processors(configured)[0], BatchSpanProcessor)
    assert configured.resource.attributes["service.name"] == "a"
    assert configured.resource.attributes[obs.TEAM] == "t"

    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    assert processors(obs.build_tracer_provider(service_name="a", service_version="1")) == ()


def _jaeger_running() -> bool:
    try:
        socket.create_connection(("localhost", 4318), timeout=0.5).close()
        return True
    except OSError:
        return False


@pytest.mark.skipif(not _jaeger_running(), reason="Jaeger is not running on localhost:4318")
def test_trace_arrives_in_jaeger(monkeypatch):
    """End to end: configured only by OTEL_EXPORTER_OTLP_ENDPOINT, a trace shows up in the local Jaeger."""
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
    monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)
    provider = obs.build_tracer_provider(service_name="sdk-test-agent", service_version="0.0.1")
    app = EnterpriseAgentApp(_graph(answer), name="sdk-test-agent", tracer_provider=provider)
    trace_id = TestClient(app.asgi).post("/invocations", json={"prompt": "hi"}).headers[TRACE_ID_HEADER]
    provider.force_flush()

    for _ in range(20):  # Jaeger indexes asynchronously
        response = httpx.get(f"http://localhost:16686/api/traces/{trace_id}", timeout=2)
        if response.status_code == 200 and response.json().get("data"):
            break
        time.sleep(0.25)
    names = {span["operationName"] for span in response.json()["data"][0]["spans"]}
    assert names == {"invoke_agent sdk-test-agent", "answer"}
