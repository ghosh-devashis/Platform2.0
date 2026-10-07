from pathlib import Path

from fastapi.testclient import TestClient
from langchain_core.language_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from __PACKAGE__.agent import create_app


class FakeToolModel(GenericFakeChatModel):
    """Scripted model: asks for the echo tool, then answers."""

    def bind_tools(self, tools, **kwargs):
        return self


def test_agent_uses_its_tool_and_answers(monkeypatch):
    monkeypatch.setenv("AGENT_MANIFEST", str(Path(__file__).parents[1] / "agent.yaml"))
    model = FakeToolModel(messages=iter([
        AIMessage(content="", tool_calls=[{"name": "echo", "args": {"text": "ping"}, "id": "call-1"}]),
        AIMessage(content="The tool said ping."),
    ]))
    response = TestClient(create_app(model).asgi).post("/invocations", json={"prompt": "Say ping"})
    assert response.status_code == 200
    assert response.json() == {"output": "The tool said ping."}
