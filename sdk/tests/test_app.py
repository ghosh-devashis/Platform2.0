"""Tests for EnterpriseAgentApp's AgentCore runtime contract (SDK-01)."""

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from langgraph.graph import END, START, MessagesState, StateGraph

from ent_agent_sdk import EnterpriseAgentApp
from ent_agent_sdk.app import SESSION_HEADER


def _graph(node):
    builder = StateGraph(MessagesState)
    builder.add_node("node", node)
    builder.add_edge(START, "node")
    builder.add_edge("node", END)
    return builder.compile()


def echo(state: MessagesState) -> dict:
    return {"messages": [AIMessage(content=f"echo: {state['messages'][-1].content}")]}


def fail(state: MessagesState) -> dict:
    raise RuntimeError("boom")


def client_for(node) -> TestClient:
    return TestClient(EnterpriseAgentApp(_graph(node), name="test-agent", version="1.2.3").asgi)


def test_ping_reports_healthy():
    response = client_for(echo).get("/ping")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "Healthy"
    assert isinstance(body["time_of_last_update"], int)


def test_standard_prompt_returns_output():
    response = client_for(echo).post("/invocations", json={"prompt": "hello"})
    assert response.status_code == 200
    assert response.json() == {"output": "echo: hello"}


def test_session_header_is_echoed():
    response = client_for(echo).post("/invocations", json={"prompt": "hi"}, headers={SESSION_HEADER: "abc-123"})
    assert response.json()["session_id"] == "abc-123"


def test_invalid_json_is_rejected():
    response = client_for(echo).post("/invocations", content=b"not json")
    assert response.status_code == 400
    assert response.json()["error"]["type"] == "InvalidRequest"


def test_non_object_body_is_rejected():
    response = client_for(echo).post("/invocations", json=["a", "list"])
    assert response.status_code == 400


def test_agent_error_returns_500_without_leaking_details():
    response = client_for(fail).post("/invocations", json={"prompt": "hi"})
    assert response.status_code == 500
    error = response.json()["error"]
    assert error["type"] == "AgentError"
    assert "boom" not in error["message"]
