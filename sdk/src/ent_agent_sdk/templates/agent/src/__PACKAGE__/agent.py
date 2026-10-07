"""__NAME__: a LangGraph agent on the Enterprise Agent SDK.

Rules this file follows (CI enforces them): the model comes from `get_model` (never a provider client), tools are
registered with `@tools.tool`, secrets come from `secrets.get`, and the app starts through `EnterpriseAgentApp`.

Which model the agent uses is set in `agent.yaml` (`models:`); `get_model()` reads it, so it is not repeated here.
"""

import os

from langchain_core.messages import SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition

from ent_agent_sdk import EnterpriseAgentApp, tools
from ent_agent_sdk.models import get_model

SYSTEM_PROMPT = "You are a helpful assistant for __TEAM__. Use the tools when they help."


@tools.tool(owner="__TEAM__", data_classification="__CLASSIFICATION__")
def echo(text: str) -> str:
    """Return the text unchanged. Replace this with a real tool."""
    return text


TOOLS = [echo]


def build_graph(model=None):
    llm = tools.bind(model or get_model(), TOOLS)  # the model named first under `models:` in agent.yaml

    def assistant(state: MessagesState, config: RunnableConfig) -> dict:
        return {"messages": [llm.invoke([SystemMessage(content=SYSTEM_PROMPT), *state["messages"]], config)]}

    builder = StateGraph(MessagesState)
    builder.add_node("assistant", assistant)
    builder.add_node("tools", ToolNode(TOOLS))
    builder.add_edge(START, "assistant")
    builder.add_conditional_edges("assistant", tools_condition)
    builder.add_edge("tools", "assistant")
    return builder.compile()


def create_app(model=None) -> EnterpriseAgentApp:
    # Name, team, data classification and guardrail profile come from agent.yaml.
    return EnterpriseAgentApp.from_manifest(
        build_graph(model), os.environ.get("AGENT_MANIFEST", "agent.yaml"), version="0.1.0"
    )
