# @enterprise/agent-sdk (TypeScript)

The TypeScript port of the Enterprise Agent SDK (requirement SDK-15). A TypeScript agent and a Python agent
(`ent-agent-sdk`) are interchangeable to callers: same endpoints, request/response shapes, headers, error types, span
tree, attribute names, audit events and manifest rules.

Built on LangGraph JS / LangChain JS, OpenTelemetry JS, `zod`, `yaml` and the AWS SDK v3. Node 22+ (developed on 24).

## Build, test, pack

```powershell
npm install
npm run build        # tsc (strict, ESM, ES2023) -> dist/
npm test             # tsc, then node --test on dist/test/*.test.js
npm run pack         # tsc, then npm pack -> enterprise-agent-sdk-0.1.0.tgz
npm run hello-agent  # example on :8080 (PORT overrides)
```

## Usage

```ts
import { EnterpriseAgentApp } from "@enterprise/agent-sdk";

const app = new EnterpriseAgentApp(graph, { name: "hello-agent", version: "0.1.0", team: "sales" });
await app.run(); // serves on 0.0.0.0:8080 (PORT overrides), JSON logs, SIGTERM/SIGINT graceful shutdown
```

`graph` is a compiled LangGraph JS graph using `MessagesAnnotation`. See `examples/hello-agent/src/index.ts`.

- `GET /ping` -> `{"status":"Healthy"|"HealthyBusy","time_of_last_update":<unix s>}`; 503 `Unhealthy` when the optional
  `healthCheck` returns false or throws.
- `POST /invocations` -> `{"prompt": "..."}` becomes `{messages:[HumanMessage]}`; response `{"output": "...", "session_id"}`.
  Override with `inputMapper` / `outputMapper`.
- Session header `x-amzn-bedrock-agentcore-runtime-session-id` becomes LangGraph `thread_id`; user header
  `x-amzn-bedrock-agentcore-runtime-user-id` goes to audit events; the response carries `x-ent-trace-id`; incoming W3C
  `traceparent` is continued.
- Errors: 400 `InvalidRequest`, 400 `GuardrailBlocked` (input) / 502 (output or tool), 500 `AgentError` (error type only).
- `EnterpriseAgentApp.fromManifest(graph, "agent.yaml")` takes name, team, classification and guardrail profile from the manifest.
- `app.listen(port)` / `app.handler` / `await app.shutdown()` are available for tests and custom hosting.
  Options: `healthCheck`, `shutdownTimeoutMs` (default 30000), `tracerProvider`, `guardrails`.

### Observability

One trace per invocation, implemented as a LangChain callback handler (`observability.GraphTracer`):

```
invoke_agent <agent>
  `- <node>
       |- chat <model>
       `- execute_tool <name>
```

Attribute names are the Python SDK's (`gen_ai.operation.name`, `gen_ai.agent.name`, `ent.team`, `ent.outcome`,
`fiddler.span.type`, ...). Prompt/response/error-message content is never recorded. Export is configured only through
`OTEL_EXPORTER_OTLP_ENDPOINT` (unset = no export), `OTEL_EXPORTER_OTLP_HEADERS`, `OTEL_RESOURCE_ATTRIBUTES`;
`OTEL_SDK_DISABLED=true` turns it off.

### Secrets

```ts
import { secrets } from "@enterprise/agent-sdk";
const key = await secrets.get("crm/api", { key: "api_key" });   // JSON field
const url = await secrets.get("ssm:/crm/db-url");                // SSM Parameter Store
await secrets.get("crm/api", { refresh: true });                 // skip the 5 minute cache
```

Fails closed (`SecretsError`, `SecretNotFoundError`), honours `AWS_ENDPOINT_URL` (Floci), 2 s connect / 5 s request
timeouts, 3 attempts.

### Models

```ts
import { getModel } from "@enterprise/agent-sdk";
const llm = getModel("chat-default");   // ChatOpenAI at ENT_MODEL_GATEWAY_URL (default http://localhost:8788/v1)
```

`maxRetries: 0`; gateway URL, key, retries and headers cannot be overridden (`ModelConfigError`). Each request carries
`x-portkey-trace-id`, `x-portkey-metadata` and `traceparent` from the active span.

### Tools, guardrails, audit

```ts
import { defineTool, tools } from "@enterprise/agent-sdk";
import { z } from "zod";

const lookup = defineTool({
  name: "lookup_customer", description: "Look up a customer by ID.",
  schema: z.object({ customerId: z.string() }),
  owner: "crm-team", dataClassification: "confidential", sideEffects: false,
  timeoutMs: 30_000, retries: 1,
  func: async ({ customerId }) => `customer ${customerId}`,
});
const llmWithTools = tools.bind(getModel("chat-default"), [lookup]);   // unregistered tools are refused
```

Guardrails (`standard` default, `strict` for restricted data) run at `input`, `tool_input`, `tool_output` and `output`;
`Guardrails.off({ waiver })` needs a waiver ID. Audit events (guardrail block/redact, side-effect tool calls, waivers)
go to a separate JSON stream (stdout or `ENT_AUDIT_LOG_PATH`) and never contain content.

CLI: `ent-agent-ts manifest validate agent.yaml`, `ent-agent-ts metadata [write [path]|read]`.

## Parity and differences with the Python SDK

Same: routes, status codes, JSON bodies (byte-identical for the contract test cases), headers, guardrail regexes,
profiles and redaction format, audit event shape, manifest rules and messages, metadata JSON field names, span tree and attribute names.
Verified by `test/contract.test.ts`, which runs both hello-agents side by side.

Differences:

| Area | Difference |
|------|-----------|
| Naming | camelCase API (`healthCheck`, `inputMapper`, `fromManifest`); durations in milliseconds (`shutdownTimeoutMs`, `timeoutMs`) instead of seconds. YAML/JSON wire keys stay snake_case. |
| Secrets | Single async `get` (no `aget`). |
| Tools | `defineTool({...zod schema...})` instead of a decorator; timeouts cannot kill running code (the call is abandoned and an `AbortSignal` is passed to `func`). |
| Errors | `AgentError` message uses the JS error class name (`RangeError`), not the Python one. |
| Guardrails | No `BedrockGuardrail` (`@aws-sdk/client-bedrock-runtime` is not an approved dependency); `extraChecks` are synchronous. JS `\d`/`\b` are ASCII-only (Python's match Unicode digits). |
| Tracing | OTLP exporter uses JSON over HTTP (Python: protobuf). Context propagation uses a small AsyncLocalStorage context manager inside the SDK (the standard package is not on the approved list); `tracestate` from callers is not continued, only `traceparent`. |
| Metadata | `frameworks` lists the npm packages; `runtime`/`node` replace `python`. |

### Also in the TypeScript SDK (matches Python)

- Manifest keys `token_budget` (`{soft?, hard?}`), `resources` (`buckets`, `tables`, `memories`) and `memory`
  (`{backend, table?}`) with the same validation and messages; parsed as `tokenBudget`, `resources`, `memory`.
- Model requests carry `x-ent-guardrail-profile` (only when guardrails are enabled) and `x-ent-data-classification`,
  even outside a trace, in addition to the trace headers. `fromManifest` sets the classification from the manifest.
- Token budget (SDK-12): `tokenBudget: {soft?, hard?}` (or `token_budget` via `fromManifest`) counts model usage,
  refuses the next model call at the hard limit (HTTP 429 `BudgetExceeded`) and audits `budget_soft_limit` /
  `budget_exceeded` with the same event shape as Python.

### Not ported

- Approvals (human-in-the-loop tool approval, HTTP 202 `approval_required`) and the `resume` flow.
- Memory backends and the checkpointer wiring (the manifest `memory` / `resources` keys are validated and parsed, but
  nothing provisions or uses them).
- Identity (`identity` module: AgentCore/STS tokens).
- Dev tools: `ent-agent new`, `dev seed`, `lint`.
- `BedrockGuardrail` (dependency not approved).
- The Python SDK's gateway-failure mapping (HTTP 503 `GatewayUnavailable`) and its `health` module.

