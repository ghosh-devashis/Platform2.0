"""Tests for approvals (SDK-10), memory (SDK-11), budgets (SDK-12), health and gateway errors (SDK-16), manifest additions."""

import http.server
import socket
import threading
import uuid

import boto3
import httpx2
import openai
import pytest
from fastapi.testclient import TestClient
from langchain_core.language_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition

from ent_agent_sdk import EnterpriseAgentApp, audit, health, manifest, memory
from ent_agent_sdk.budget import TokenBudget
from ent_agent_sdk.tools import ToolRegistry

SESSION = "approval-session-0123456789-0123456789"
HEADER = "x-amzn-bedrock-agentcore-runtime-session-id"


@pytest.fixture
def events():
    captured = []
    audit.add_sink(captured.append)
    yield captured
    audit.remove_sink(captured.append)


class Scripted(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


def tool_call(name, args, call_id="call-1"):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id}])


def agent_with_tools(registry, messages, **app_kwargs):
    model = Scripted(messages=iter(messages))

    def assistant(state: MessagesState):
        return {"messages": [model.invoke(state["messages"])]}

    builder = StateGraph(MessagesState)
    builder.add_node("assistant", assistant)
    builder.add_node("tools", ToolNode(registry.all()))
    builder.add_edge(START, "assistant")
    builder.add_conditional_edges("assistant", tools_condition)
    builder.add_edge("tools", "assistant")
    return TestClient(EnterpriseAgentApp(builder.compile(), name="a", team="t", **app_kwargs).asgi)


def refund_registry():
    registry, calls = ToolRegistry(), []

    @registry.tool(owner="payments", data_classification="confidential", side_effects=True, name="refund")
    def refund(order_id: str) -> str:
        """Refund an order."""
        calls.append(order_id)
        return f"refunded {order_id}"

    return registry, calls


# --- SDK-10 approvals ---------------------------------------------------------------------------------------

def test_side_effect_tool_pauses_for_approval_then_runs_when_approved(events):
    registry, calls = refund_registry()
    client = agent_with_tools(registry, [tool_call("refund", {"order_id": "o-1"}), AIMessage(content="Refunded.")])

    first = client.post("/invocations", json={"prompt": "refund o-1"}, headers={HEADER: SESSION})
    assert first.status_code == 202
    body = first.json()
    assert body["status"] == "approval_required" and body["session_id"] == SESSION
    assert body["approvals"] == [{"type": "tool_approval", "tool": "refund", "owner": "payments",
                                  "data_classification": "confidential", "arguments": {"order_id": "o-1"}}]
    assert calls == []  # nothing ran yet

    second = client.post("/invocations", json={"resume": {"approved": True, "approver": "alice"}}, headers={HEADER: SESSION})
    assert second.status_code == 200 and second.json()["output"] == "Refunded."
    assert calls == ["o-1"]
    approvals = [e for e in events if e["event_type"] == audit.TOOL_APPROVAL]
    assert [e["outcome"] for e in approvals] == ["requested", "approved"]
    assert approvals[1]["details"]["approver"] == "alice"


def test_rejected_approval_means_the_tool_never_runs(events):
    registry, calls = refund_registry()
    client = agent_with_tools(registry, [tool_call("refund", {"order_id": "o-2"}), AIMessage(content="Okay, not refunded.")])
    assert client.post("/invocations", json={"prompt": "refund"}, headers={HEADER: SESSION}).status_code == 202
    answer = client.post("/invocations", json={"resume": {"approved": False}}, headers={HEADER: SESSION})
    assert answer.status_code == 200 and answer.json()["output"] == "Okay, not refunded."
    assert calls == []
    assert [e["outcome"] for e in events if e["event_type"] == audit.TOOL_APPROVAL] == ["requested", "rejected"]


def test_resume_needs_the_session_header():
    registry, _ = refund_registry()
    client = agent_with_tools(registry, [AIMessage(content="x")])
    response = client.post("/invocations", json={"resume": {"approved": True}})
    assert response.status_code == 400 and response.json()["error"]["type"] == "InvalidRequest"


def test_approval_request_without_session_gets_a_generated_session_id():
    registry, _ = refund_registry()
    client = agent_with_tools(registry, [tool_call("refund", {"order_id": "o-3"}), AIMessage(content="done")])
    first = client.post("/invocations", json={"prompt": "refund"}).json()
    session_id = first["session_id"]
    assert len(session_id) >= 33
    assert client.post("/invocations", json={"resume": {"approved": True}}, headers={HEADER: session_id}).status_code == 200


def test_approval_registration_rules(events):
    registry = ToolRegistry()

    @registry.tool(owner="t", side_effects=True, name="a")
    def a() -> str:
        """x"""
        return "x"

    assert registry.metadata("a").requires_approval is True
    with pytest.raises(ValueError, match="waiver"):
        registry.tool(owner="t", side_effects=True, requires_approval=False, name="b")
    with pytest.raises(ValueError):
        registry.tool(owner="t", requires_approval=True, name="c")  # read-only tools have nothing to approve

    @registry.tool(owner="t", side_effects=True, requires_approval=False, approval_waiver="W-9", name="d")
    def d() -> str:
        """x"""
        return "x"

    assert registry.metadata("d").requires_approval is False
    assert any(e["event_type"] == audit.WAIVER_IN_EFFECT and e["details"]["waiver"] == "W-9" for e in events)


# --- SDK-11 memory ------------------------------------------------------------------------------------------

def counting_graph():
    def count(state: MessagesState):
        return {"messages": [AIMessage(content=f"seen {len(state['messages'])}")]}

    builder = StateGraph(MessagesState)
    builder.add_node("count", count)
    builder.add_edge(START, "count")
    return builder.compile()


def test_sessions_continue_and_calls_without_a_session_are_independent():
    client = TestClient(EnterpriseAgentApp(counting_graph(), name="m").asgi)
    h = {HEADER: SESSION}
    assert client.post("/invocations", json={"prompt": "a"}, headers=h).json()["output"] == "seen 1"
    assert client.post("/invocations", json={"prompt": "b"}, headers=h).json()["output"] == "seen 3"  # continues
    assert client.post("/invocations", json={"prompt": "c"}).json()["output"] == "seen 1"  # no session: fresh
    assert client.post("/invocations", json={"prompt": "d"}).json()["output"] == "seen 1"
    assert client.post("/invocations", json={"prompt": "e"}, headers={HEADER: "other-" + SESSION}).json()["output"] == "seen 1"


def test_checkpointer_none_means_stateless():
    client = TestClient(EnterpriseAgentApp(counting_graph(), name="m", checkpointer=None).asgi)
    h = {HEADER: SESSION}
    assert client.post("/invocations", json={"prompt": "a"}, headers=h).json()["output"] == "seen 1"
    assert client.post("/invocations", json={"prompt": "b"}, headers=h).json()["output"] == "seen 1"


def test_environment_selects_the_checkpointer(monkeypatch):
    assert memory.from_environment() is None
    monkeypatch.setenv("ENT_CHECKPOINTER", "memory")
    assert type(memory.from_environment()).__name__ == "InMemorySaver"
    monkeypatch.setenv("ENT_CHECKPOINTER", "carrier-pigeon")
    with pytest.raises(ValueError):
        memory.from_environment()


def _floci_running() -> bool:
    try:
        socket.create_connection(("localhost", 4566), timeout=0.5).close()
        return True
    except OSError:
        return False


@pytest.mark.skipif(not _floci_running(), reason="Floci is not running on localhost:4566")
def test_dynamodb_checkpointer_survives_a_restart():
    """Two separate savers on one table = a process restart: the session history comes back from DynamoDB."""
    session = boto3.session.Session(aws_access_key_id="test", aws_secret_access_key="test", region_name="us-east-1")
    table = f"ent-test-{uuid.uuid4().hex[:8]}"
    endpoint = "http://localhost:4566"
    try:
        first = memory.dynamodb(table, create=True, session=session, endpoint_url=endpoint)
        app1 = TestClient(EnterpriseAgentApp(counting_graph(), name="m", checkpointer=first).asgi)
        h = {HEADER: SESSION}
        assert app1.post("/invocations", json={"prompt": "a"}, headers=h).json()["output"] == "seen 1"

        restarted = memory.dynamodb(table, session=session, endpoint_url=endpoint)  # new process, empty memory
        app2 = TestClient(EnterpriseAgentApp(counting_graph(), name="m", checkpointer=restarted).asgi)
        assert app2.post("/invocations", json={"prompt": "b"}, headers=h).json()["output"] == "seen 3"
        assert app2.post("/invocations", json={"prompt": "z"}, headers={HEADER: "new-" + SESSION}).json()["output"] == "seen 1"

        restarted.delete_thread(SESSION)
        fresh = memory.dynamodb(table, session=session, endpoint_url=endpoint)
        app3 = TestClient(EnterpriseAgentApp(counting_graph(), name="m", checkpointer=fresh).asgi)
        assert app3.post("/invocations", json={"prompt": "c"}, headers=h).json()["output"] == "seen 1"  # deleted
    finally:
        session.client("dynamodb", endpoint_url=endpoint).delete_table(TableName=table)


# --- SDK-12 budgets -----------------------------------------------------------------------------------------

def two_call_graph(tokens_in=8, tokens_out=4):
    def reply():
        return AIMessage(content="hi", usage_metadata={"input_tokens": tokens_in, "output_tokens": tokens_out,
                                                       "total_tokens": tokens_in + tokens_out})

    model = GenericFakeChatModel(messages=iter([reply(), reply(), reply()]))

    def step(state: MessagesState, config: RunnableConfig):
        return {"messages": [model.invoke(state["messages"], config)]}

    builder = StateGraph(MessagesState)
    builder.add_node("one", step)
    builder.add_node("two", step)
    builder.add_edge(START, "one")
    builder.add_edge("one", "two")
    return builder.compile()


def test_hard_budget_stops_the_next_model_call(events):
    app = EnterpriseAgentApp(two_call_graph(), name="b", token_budget=TokenBudget(soft=5, hard=10), checkpointer=None)
    response = TestClient(app.asgi).post("/invocations", json={"prompt": "hi"})
    assert response.status_code == 429
    assert response.json()["error"]["type"] == "BudgetExceeded"
    kinds = [e["event_type"] for e in events]
    assert audit.BUDGET_SOFT in kinds and audit.BUDGET_EXCEEDED in kinds


def test_soft_budget_warns_once_and_lets_the_run_finish(events):
    app = EnterpriseAgentApp(two_call_graph(3, 2), name="b", token_budget=TokenBudget(soft=4, hard=1000), checkpointer=None)
    assert TestClient(app.asgi).post("/invocations", json={"prompt": "hi"}).status_code == 200
    assert [e["event_type"] for e in events].count(audit.BUDGET_SOFT) == 1
    assert audit.BUDGET_EXCEEDED not in [e["event_type"] for e in events]


def test_budget_validation_and_manifest_wiring():
    with pytest.raises(ValueError):
        TokenBudget(soft=10, hard=5)
    with pytest.raises(ValueError):
        TokenBudget(hard=0)
    parsed = manifest.parse({"name": "b-agent", "team": "t", "data_classification": "internal",
                             "token_budget": {"soft": 100, "hard": 200}})
    app = EnterpriseAgentApp.from_manifest(counting_graph(), parsed)
    assert app.token_budget == TokenBudget(soft=100, hard=200)


# --- SDK-16 health and gateway errors -----------------------------------------------------------------------

class _Handler(http.server.BaseHTTPRequestHandler):
    status = 200

    def do_GET(self):  # noqa: N802
        self.send_response(type(self).status)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):  # silence the test server
        pass


def serve(status):
    handler = type("H", (_Handler,), {"status": status})
    server = http.server.HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}/v1"


def test_gateway_reachability():
    server, url = serve(200)
    try:
        assert health.gateway_reachable(url) is True
    finally:
        server.shutdown()
    assert health.gateway_reachable("http://127.0.0.1:9/v1", timeout=0.3) is False
    server, url = serve(503)
    try:
        assert health.gateway_reachable(url) is False
    finally:
        server.shutdown()


def test_gateway_check_is_cached():
    server, url = serve(200)
    check = health.gateway_check(url, ttl=60)
    assert check() is True
    server.shutdown()
    server.server_close()
    assert check() is True  # served from the cache: no second request was made


def test_manifest_with_models_installs_the_gateway_health_check(monkeypatch):
    parsed = manifest.parse({"name": "h-agent", "team": "t", "data_classification": "internal", "models": ["chat-default"]})
    assert EnterpriseAgentApp.from_manifest(counting_graph(), parsed).health_check is not None
    monkeypatch.setenv("ENT_HEALTH_CHECK_GATEWAY", "false")
    assert EnterpriseAgentApp.from_manifest(counting_graph(), parsed).health_check is None
    no_models = manifest.parse({"name": "h-agent", "team": "t", "data_classification": "internal"})
    monkeypatch.delenv("ENT_HEALTH_CHECK_GATEWAY")
    assert EnterpriseAgentApp.from_manifest(counting_graph(), no_models).health_check is None


def failing_graph(exc):
    def boom(state: MessagesState):
        raise exc

    builder = StateGraph(MessagesState)
    builder.add_node("boom", boom)
    builder.add_edge(START, "boom")
    return builder.compile()


REQUEST = httpx2.Request("POST", "http://gateway.test/v1/chat/completions")


@pytest.mark.parametrize("exc", [
    openai.APIConnectionError(request=REQUEST),
    openai.APITimeoutError(request=REQUEST),
    openai.InternalServerError("bad gateway", response=httpx2.Response(502, request=REQUEST), body=None),
])
def test_gateway_outage_is_one_clear_error(exc):
    response = TestClient(EnterpriseAgentApp(failing_graph(exc), name="g", checkpointer=None).asgi).post(
        "/invocations", json={"prompt": "hi"})
    assert response.status_code == 503
    assert response.json()["error"]["type"] == "GatewayUnavailable"


def test_other_provider_errors_stay_generic():
    exc = openai.AuthenticationError("bad key", response=httpx2.Response(401, request=REQUEST), body=None)
    response = TestClient(EnterpriseAgentApp(failing_graph(exc), name="g", checkpointer=None).asgi).post(
        "/invocations", json={"prompt": "hi"})
    assert response.status_code == 500 and response.json()["error"]["type"] == "AgentError"


# --- manifest additions -------------------------------------------------------------------------------------

def test_manifest_resources_memory_and_budget_validation():
    good = {"name": "r-agent", "team": "t", "data_classification": "internal",
            "token_budget": {"hard": 5000},
            "resources": {"buckets": ["agent-files"], "tables": [{"name": "notes", "partition_key": "id"}],
                          "memories": ["agent_memory"]},
            "memory": {"backend": "dynamodb", "table": "agent-checkpoints"}}
    parsed = manifest.parse(good)
    assert parsed.resources["buckets"] == ["agent-files"] and parsed.memory["backend"] == "dynamodb"
    for change, expected in [
        ({"token_budget": {"soft": 10, "hard": 5}}, "'soft' must not exceed"),
        ({"token_budget": {"hard": "lots"}}, "positive whole numbers"),
        ({"token_budget": {}}, "'token_budget'"),
        ({"resources": {"buckets": ["Bad Bucket"]}}, "bucket name"),
        ({"resources": {"tables": [{"name": "x"}]}}, "partition_key"),
        ({"resources": {"queues": []}}, "'resources' may only"),
        ({"memory": {"backend": "redis"}}, "'memory' needs"),
    ]:
        with pytest.raises(manifest.ManifestError) as info:
            manifest.parse({**good, **change})
        assert expected in str(info.value), change
