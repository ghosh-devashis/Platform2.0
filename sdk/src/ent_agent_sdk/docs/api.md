# Enterprise Agent SDK: API summary

Everything an agent needs, in one page. Install: depend on `ent-agent-sdk` only (it brings the approved LangGraph,
LangChain, FastAPI and AWS versions). Never add those yourself.

## overview

```python
from langgraph.graph import START, MessagesState, StateGraph
from ent_agent_sdk import EnterpriseAgentApp, tools
from ent_agent_sdk.models import get_model

@tools.tool(owner="crm-team", data_classification="internal")
def lookup(customer_id: str) -> str:
    """Look up a customer."""
    ...

def build_graph():
    llm = tools.bind(get_model(), [lookup])   # the model comes from `models:` in agent.yaml
    ...  # normal LangGraph: nodes call llm.invoke(messages, config)

app = EnterpriseAgentApp.from_manifest(build_graph(), "agent.yaml", version="0.1.0")
app.run()   # AgentCore contract: GET /ping, POST /invocations on port 8080
```

Request `{"prompt": "..."}` returns `{"output": "...", "session_id": "..."}`. The AgentCore session header
`X-Amzn-Bedrock-AgentCore-Runtime-Session-Id` continues a conversation; without it each call is independent.

## models

`get_model()` returns a LangChain chat model that talks only to the LLM gateway. With no name it uses the **first model
listed under `models:` in `agent.yaml`**, so the choice is made in one place and never hardcoded in code (the manifest is
found through `AGENT_MANIFEST`, else `agent.yaml` in the working directory). An agent that really uses several models
passes a name (`get_model("chat-fast")`, a name from `declared_models()`); `declared_models()` returns the `models:` list in
order. Names come from the approved registry (`chat-default`, `chat-fast`). Provider, model and fallbacks are
central configuration. Do not construct `ChatOpenAI`, `ChatAnthropic`, `ChatBedrock` or provider clients (rules ENT-001..003).
The SDK does not retry model calls: the gateway does.

## tools

`@tools.tool(owner=..., data_classification="public|internal|confidential|restricted", side_effects=False)`.
Registered tools get guardrail checks on arguments and results, a 30 s timeout, one retry (read-only tools only), trace
attributes and, for `side_effects=True`, an audit event per call **and human approval before running**. Bind with
`tools.bind(model, [tool_a, tool_b])` (unregistered tools are refused). Raw LangChain `@tool` is not allowed (ENT-007).
Switching approval off needs an approved waiver: `requires_approval=False, approval_waiver="WAIVER-123"` (ENT-009).

## approvals

A side-effect tool pauses the run. The caller receives HTTP 202
`{"status": "approval_required", "approvals": [{"tool": ..., "arguments": {...}}], "session_id": "..."}`. Answer on the
same session: `POST /invocations {"resume": {"approved": true, "approver": "alice"}}` (header `X-Amzn-Bedrock-AgentCore-Runtime-Session-Id`).
A declined action returns "Not approved..." to the model, which can explain it to the user.

## secrets

`secrets.get("name")`, `secrets.get("name", key="field")` for JSON secrets, `secrets.get("ssm:/path")`, `await secrets.aget(...)`,
`refresh=True` to bypass the 5-minute cache. A missing secret raises `SecretNotFoundError`; there are no defaults.
Never read credentials from environment variables (ENT-004) or call Secrets Manager directly (ENT-005).

## guardrails

On by default (`standard` profile; `strict` for restricted data): PII, secrets and prompt injection are redacted or
blocked at input, tool input, tool output and the final answer. Blocked input returns 400 `GuardrailBlocked`; a
withheld answer returns 502. Disabling needs `Guardrails.off(waiver="WAIVER-123")` with an approved entry in `waivers.yaml` (ENT-008).
Add Amazon Bedrock Guardrails with `Guardrails(extra_checks=[BedrockGuardrail("<id>", "<version>")])`.

## budget

`token_budget: {soft: 20000, hard: 50000}` in `agent.yaml` (or `EnterpriseAgentApp(token_budget=TokenBudget(...))`).
Soft limit: audit + trace event once. Hard limit: the next model call is refused and the caller gets 429 `BudgetExceeded`.

## memory

Sessions are remembered per session header. Storage: in memory by default; set `memory: {backend: dynamodb, table: agent-checkpoints}`
in `agent.yaml` (or `ENT_CHECKPOINTER=dynamodb`) for persistence that survives restarts. `AWS_ENDPOINT_URL` points it at Floci locally.

## identity

`identity.configure(identity.AgentCoreIdentity(credential_provider="crm-oauth", workload_name="my-agent"))`, then
`identity.bearer_headers(scopes=["crm.read"])` for outbound calls. `StsIdentity(role_arn=...)` gives short-lived AWS
credentials. Never use static API keys.

## manifest

`agent.yaml`: `name`, `team`, `data_classification`, optional `guardrail_profile` (restricted requires `strict`), `description`,
`models`, `tools`, `secrets`, `token_budget`, `memory`, `resources` (local buckets/tables/memories to seed).
Validate with `ent-agent manifest validate`; check everything with `ent-agent check`.

## observability

Every call is traced automatically (agent, graph steps, model calls, tools) with OpenTelemetry. Set
`OTEL_EXPORTER_OTLP_ENDPOINT` (for example `http://localhost:4318` for the local Jaeger). Logs are JSON with trace IDs.
Responses carry `x-ent-trace-id`. Prompts, answers and error messages are never recorded on spans.

## testing

Use a scripted fake model so tests need no keys or network:

```python
class Scripted(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):
        return self

model = Scripted(messages=iter([AIMessage(content="hello")]))
client = TestClient(create_app(model).asgi)   # fastapi.testclient
assert client.post("/invocations", json={"prompt": "hi"}).json()["output"] == "hello"
```

For end-to-end tests against the local stack, use the router's replay mode (`ROUTER_MODE=replay`) and commit recordings.
