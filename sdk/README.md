# ent-agent-sdk

The Enterprise Agent SDK. Every agent in the enterprise is built on it.

## What it does today (v0.1)

- **`EnterpriseAgentApp`** (SDK-01) serves a compiled LangGraph graph on the
  [Bedrock AgentCore runtime contract](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-getting-started.html):
  - `GET /ping` → `{"status": "Healthy" | "HealthyBusy", "time_of_last_update": <unix seconds>}`
  - `POST /invocations` → runs the graph on the request body
  - port `8080`; the AgentCore session header is passed to the graph as the LangGraph `thread_id`
- **Approved framework versions** (SDK-02) are pinned in `pyproject.toml`.
- **Observability** (SDK-03/04): automatic OpenTelemetry traces + JSON logs (see [Observability](#observability)).
- **`secrets.get()`** (SDK-05) fetches secrets at runtime from AWS Secrets Manager or SSM Parameter Store
  (see [Secrets](#secrets)).

## Standard request / response

| | Shape |
|---|---|
| Request | `{"prompt": "..."}` (other fields are allowed and passed through) |
| Response | `{"output": "..."}` (the last message's text), plus `session_id` |

`{"prompt": ...}` becomes `{"messages": [HumanMessage(prompt)]}`, so graphs use LangGraph's `MessagesState`.
Agents can override this with `input_mapper` / `output_mapper`.

## Usage

```python
from ent_agent_sdk import EnterpriseAgentApp

app = EnterpriseAgentApp(graph, name="my-agent", version="0.1.0")
app.run()  # serves on 0.0.0.0:8080
```

## Secrets

Never read secrets from code, env vars or files. Fetch them at runtime:

```python
from ent_agent_sdk import secrets

api_key = secrets.get("crm/api", key="api_key")   # one field of a JSON secret (Secrets Manager)
db_url = secrets.get("ssm:/crm/db-url")           # SSM Parameter Store (SecureString decrypted)
token = await secrets.aget("crm/token")            # in async graph nodes
secrets.get("crm/api", refresh=True)               # skip the cache, e.g. after an auth failure
```

- Values are cached for 5 minutes, so rotated secrets are picked up without a restart.
- **Fails closed**: a missing or unreadable secret raises `secrets.SecretNotFoundError` / `secrets.SecretsError`.
  There is no default value. Error messages never contain the value.
- AWS calls use short timeouts (2 s connect, 5 s read) and up to 3 attempts with backoff (SDK-16).
- Configuration is standard AWS, so the same image works everywhere:

| Where | Credentials | Endpoint |
|-------|-------------|----------|
| AgentCore / AWS | IAM role (automatic) | leave `AWS_ENDPOINT_URL` unset |
| Local (Floci) | `AWS_ACCESS_KEY_ID=test`, `AWS_SECRET_ACCESS_KEY=test`, `AWS_DEFAULT_REGION=us-east-1` | `AWS_ENDPOINT_URL=http://localhost:4566` |
## Observability

Every invocation is traced automatically; agents write no tracing code.

```
invoke_agent <agent>            agent name/version/team, session, outcome
  └─ <node>                     one span per graph node (and nested chains)
       ├─ chat <model>          model, provider, input/output tokens
       └─ execute_tool <name>   tool name
```

- Attribute names follow the OpenTelemetry GenAI semantic conventions; each span also has `fiddler.span.type`.
- **Never recorded:** prompt/response content and error messages (only the error type).
- Responses include an `x-ent-trace-id` header; incoming W3C `traceparent` headers are continued.
- `app.run()` writes JSON logs to stdout with `trace_id` / `span_id` (level from `LOG_LEVEL`).
- Where traces go is configuration only (standard OpenTelemetry settings):

| Setting | Example |
|---------|---------|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://localhost:4318` (local Jaeger; UI at http://localhost:16686). Unset = no export |
| `OTEL_EXPORTER_OTLP_HEADERS` | auth headers for a hosted backend |
| `OTEL_RESOURCE_ATTRIBUTES` | `deployment.environment=dev` |
| `OTEL_SDK_DISABLED` | `true` turns tracing off |

## Health and shutdown

```python
app = EnterpriseAgentApp(graph, name="crm-agent", version="1.0.0", team="sales",
                         health_check=lambda: True,   # False/raises -> /ping returns 503 "Unhealthy"
                         shutdown_timeout=30)          # seconds in-flight requests may finish on shutdown
```
## Models

```python
from ent_agent_sdk.models import get_model

llm = get_model()                      # a LangChain chat model: .invoke, .bind_tools, streaming
```

With no name, `get_model()` returns the **first model listed under `models:` in `agent.yaml`**: the choice is made in
one place and never hardcoded in code, so changing the model is a one-line manifest edit (the manifest is found through
`AGENT_MANIFEST`, else `agent.yaml` in the working directory). An agent that uses several models passes a name
(`get_model("chat-fast")`), or reads them in order with `declared_models()`.

Always routed through the gateway (locally: the model router in front of Portkey); provider, model and fallbacks are
central configuration. Unapproved names are rejected. The SDK doesn't retry model calls (Portkey does). Each request
carries the agent's trace ID and name/version/team so gateway logs join the agent's trace.

## Tools

```python
from ent_agent_sdk import tools

@tools.tool(owner="crm-team", data_classification="confidential", side_effects=False)
def lookup_customer(customer_id: str) -> str:
    """Look up a customer by ID."""
    ...

llm = tools.bind(get_model(), [lookup_customer])   # unregistered tools are refused
```

Registered tools get guardrail checks on arguments and results, a 30 s timeout, one bounded retry (read-only tools
only), trace attributes (owner, classification, side effects) and, for `side_effects=True`, an audit event per call.

## Guardrails and audit

`EnterpriseAgentApp` applies the `standard` guardrail profile by default (`strict` for restricted data):
PII and secrets are redacted or blocked, prompt injection in tool output is blocked, at four stages (input, tool
input, tool output, final answer). Disabling needs an approved waiver: `Guardrails.off(waiver="WAIVER-123")` plus a
matching entry in `waivers.yaml`. Audit events (guardrail blocks/redactions, side-effect tool calls, waivers in
effect) go to their own JSON stream (stdout, or the file in `ENT_AUDIT_LOG_PATH`) with agent, user, session and trace
IDs and never any content. Add `Guardrails(extra_checks=[BedrockGuardrail("<id>", "<version>")])` to layer Amazon
Bedrock Guardrails on top.

## Identity

```python
from ent_agent_sdk import identity
identity.configure(identity.AgentCoreIdentity(credential_provider="crm-oauth", workload_name="crm-agent"))
headers = identity.bearer_headers(scopes=["crm.read"])        # short-lived token, never a static key
```

`StsIdentity(role_arn=...)` gives short-lived AWS credentials. (Written from the AWS API reference; verify against a
real AWS account.)

## Manifest and project template

`agent.yaml` declares name, team, data classification, guardrail profile, models, tools and secrets.
`EnterpriseAgentApp.from_manifest(graph, "agent.yaml")` takes name, team, classification and guardrails from it.

```bash
ent-agent new crm-agent --team sales --classification confidential   # ready-to-run project (Docker, CI, tests, AI instruction files)
ent-agent manifest validate agent.yaml
ent-agent dev seed          # create the manifest's secrets and AgentCore runtime in the local Floci
ent-agent metadata          # SDK / policy-bundle versions baked into this build
```

## Approvals, budgets, memory and health

- **Human approval**: tools with `side_effects=True` pause the run. The caller gets HTTP 202 `{"status": "approval_required", "approvals": [...], "session_id": "..."}`;
  answer with `POST /invocations {"resume": {"approved": true, "approver": "alice"}}` and the same session header. A declined action goes back to the model as "Not approved...".
  Switching it off needs `requires_approval=False, approval_waiver="WAIVER-..."` (rule ENT-009).
- **Memory**: a conversation continues when the session header is sent; without it each call is independent. In memory by default; `memory: {backend: dynamodb, table: agent-checkpoints}` in `agent.yaml` (or `ENT_CHECKPOINTER=dynamodb`) persists it across restarts.
- **Token budget**: `token_budget: {soft: 20000, hard: 50000}` in `agent.yaml`: a warning once, then the next model call is refused and the caller gets 429 `BudgetExceeded`.
- **Health**: with models in the manifest, `/ping` reports `Unhealthy` (503) while the gateway is unreachable, and a gateway outage reaches callers as 503 `GatewayUnavailable`.

## Command line

```bash
ent-agent new <name> --team <team> [--classification internal]   # create a project (also records what it came from)
ent-agent update [path] [--apply] [--check]                      # pull template changes in as a reviewable diff
ent-agent check [paths] [--json]                                 # code, agent.yaml, pyproject.toml against the ENT rules
ent-agent rules [ENT-001] [--markdown]                           # list or explain the rules
ent-agent eval run evals/golden.yaml --url http://localhost:8080 # evaluation gate (golden questions, minimum scores)
ent-agent mcp                                                    # MCP standards server for AI assistants
ent-agent dev seed                                               # secrets, buckets, tables, memories and the runtime in Floci
ent-agent manifest validate | metadata
```
