# Platform 2.0 guide: how it fits together

A learning guide for people new to the platform. It explains the moving parts with diagrams. The detail (every requirement,
every function, every endpoint, every config file) is in the author's learning workbook (an Excel file that is not part of
this repository; `docs/requirements-traceability.md` and the code hold the same facts); each section below names the sheet it comes from.

> **Viewing the diagrams.** The diagrams are Mermaid. They render in GitHub and in VS Code's Markdown preview (install
> the "Markdown Preview Mermaid Support" extension if you only see code). Section 1 also has a plain-text version.

## 1. The big picture

Every agent is a normal LangGraph graph wrapped by one SDK. The SDK gives it the same HTTP contract, tracing, guardrails,
secrets, budgets and memory. All model calls go through one gateway path. CI enforces the rules before anything is built.

```text
 Developer (VS Code + AI assistant)                      Platform team
   |  ent-agent new / check, extension, MCP server          |  registry.json, policy/, templates, pipeline
   v                                                        v
 +--------------------------- one agent container (port 8080) ------------------------------+
 |  EnterpriseAgentApp (SDK)                                                                 |
 |   GET /ping   POST /invocations --> guardrails(in) --> LangGraph graph --> guardrails(out)|
 |                                          |  tools (guardrails, audit, approval, timeout)  |
 |                                          |  memory (in-memory or DynamoDB)                |
 |                                          |  secrets.get() --> Secrets Manager             |
 |                                          +-- get_model("chat-default") --+                |
 +--------------------------------------------------------------------------|----------------+
        ^ called by AgentCore (AWS) or the local invoke router (:4567)       v
                                                          model router (:8788): key check, registry, limits,
   traces (OTLP) ----------------------------> Jaeger     cache, record/replay, provider keys
                                                                            |
                                                          Portkey gateway (:8787): fallback, retry, guardrail hooks
                                                                            |
                                                                  Anthropic  ->  (fallback) OpenAI
 Local AWS stand-in: Floci (:4566) = Secrets Manager, DynamoDB, S3, STS, AgentCore control plane
```

```mermaid
flowchart LR
  subgraph Dev["Developer machine"]
    VSC["VS Code extension<br/>+ AI assistant"] --> CLI["ent-agent CLI<br/>new / check / eval / seed"]
    VSC --> MCP["MCP standards server"]
  end
  subgraph Agent["Agent container :8080"]
    SDK["EnterpriseAgentApp<br/>/ping /invocations"] --> G["LangGraph graph"]
    SDK --> GR["Guardrails<br/>input, tool, output"]
    G --> T["Tools<br/>audit, approval, timeout"]
    G --> M["get_model"]
  end
  CALLER["AgentCore or<br/>invoke router :4567"] --> SDK
  M --> MR["Model router :8788"]
  MR --> PK["Portkey :8787<br/>fallback, retry, hooks"]
  PK --> AN["Anthropic"]
  PK -.fallback.-> OA["OpenAI"]
  MR --> SM[("Secrets Manager<br/>provider keys")]
  SDK --> SM
  SDK --> DDB[("DynamoDB<br/>session memory")]
  SDK -.OTLP traces.-> J["Jaeger :16686"]
  MR -.OTLP traces.-> J
  FL["Floci :4566<br/>local AWS"] --- SM
  FL --- DDB
```

Next: workbook sheets **Integrations** and **Glossary**.

## 2. The journey of a new agent

```mermaid
flowchart TD
  A["0. Install Docker, uv, VS Code<br/>start the stack: dev.ps1 up"] --> B["1. ent-agent new name --team t<br/>template + manifest + CI + AI instructions"]
  B --> C["2. Fill in agent.yaml<br/>models, tools, secrets, budget, memory"]
  C --> D["3. Write tools and graph<br/>@tools.tool, get_model, from_manifest"]
  D --> E["ent-agent check<br/>rules ENT-001.. as you type"]
  E -->|fix findings| D
  E --> F["4. Store secrets, seed Floci<br/>ent-agent dev seed"]
  F --> G["5. Run and call /invocations<br/>read the trace in Jaeger"]
  G --> H["6. Tests: pytest with a fake model<br/>record real answers, replay them"]
  H --> I["7. Evaluation gate<br/>golden questions with minimum scores"]
  I --> J["8. Commit<br/>pre-commit fast policy checks"]
  J --> K["9. Push: agent-ci.yml<br/>policy, test, supply-chain, build"]
  K --> L["10. Publish on main<br/>push, sign with cosign, provenance"]
  L --> M["11. Deploy: Terraform<br/>rehearse on Floci, then AWS"]
  M --> N["12. Operate: update, versions,<br/>waiver register"]
```

Next: workbook sheet **Build Sequence** (commands and what happens behind the scenes for each step).

## 3. What happens on one call

```mermaid
sequenceDiagram
  autonumber
  participant C as Caller
  participant IR as Invoke router
  participant A as Agent SDK
  participant G as LangGraph graph
  participant MR as Model router
  participant SM as Secrets Manager
  participant PK as Portkey
  participant LLM as Anthropic / OpenAI
  C->>IR: POST /runtimes/ARN/invocations {prompt}
  IR->>A: POST /invocations (session + trace headers)
  A->>A: start trace, input guardrails
  A->>G: ainvoke(messages, thread_id = session)
  G->>MR: POST /v1/chat/completions (agent key, trace id, profile)
  MR->>MR: key, registry, rate limit, budget, cache or replay
  MR->>SM: provider key for the target
  MR->>PK: request + x-portkey-config (targets, hooks)
  PK->>LLM: before hooks, call, after hooks
  LLM-->>PK: answer or tool call
  PK-->>MR: OpenAI-shaped answer
  MR-->>G: answer (tokens counted, recorded or cached)
  opt model asked for a tool
    G->>G: tool guardrails, audit, approval pause (HTTP 202) or run
    G->>MR: next model call with the tool result
  end
  G-->>A: final state
  A->>A: output guardrails, close span
  A-->>C: 200 {output, session_id} + x-ent-trace-id
```

Errors you can meet on this path: 400 `GuardrailBlocked`, 429 `BudgetExceeded`, 502 `GuardrailBlocked`, 503 `GatewayUnavailable`,
500 `AgentError` (sheet **Agent Errors**). Next: sheets **Request Flow** and **API Contracts**.

### 3.1 Inside the model router

```mermaid
flowchart TD
  S["POST /v1/chat/completions"] --> K{"valid gateway key?"}
  K -->|no| E401["401"]
  K -->|yes| R{"model in registry?"}
  R -->|no| E400["400 model_not_approved"]
  R -->|yes| N["normalize token limit<br/>max_tokens to max_completion_tokens,<br/>apply model default"]
  N --> RL{"rate limit or<br/>daily budget hit?"}
  RL -->|yes| E429["429"]
  RL -->|no| MODE{"ROUTER_MODE"}
  MODE -->|replay| REC{"recording found?"}
  REC -->|yes| OUT["return recorded answer"]
  REC -->|no| MISS["stub, or 404 in CI"]
  MODE -->|real, record, local| CA{"cache hit?<br/>opted in, non-sensitive"}
  CA -->|yes| OUT
  CA -->|no| KEY["read provider keys,<br/>build Portkey config + guardrail hooks"]
  KEY --> PKY["send to Portkey<br/>next instance only on connection errors"]
  PKY --> HK{"guardrail denied?<br/>HTTP 446"}
  HK -->|yes| E4["400 or 502 guardrail_blocked"]
  HK -->|no| FIN["strip hook_results, count tokens,<br/>record or cache, return"]
```

## 4. How the pipeline enforces the rules

```mermaid
flowchart LR
  PC["pre-commit<br/>fast policy subset"] --> PUSH["git push"]
  PUSH --> P["Policy gates<br/>gitleaks, Semgrep, Conftest,<br/>waivers, pipeline, versions"]
  P --> T["Tests<br/>unit tests +<br/>evaluation gate in replay mode"]
  T --> SC["Supply chain<br/>vulnerability audit, SAST,<br/>SBOM, licences"]
  SC --> B["Build image"]
  B --> PUB{"main branch and<br/>publish = true?"}
  PUB -->|yes| SIGN["push multi-arch, cosign sign,<br/>build provenance"]
  PUB -->|no| END["stop: image not published"]
```

Next: workbook sheets **Config Files** (policy and pipeline files) and **Requirements Traceability** (POL-xx rows).

## 5. Where configuration and secrets live

```mermaid
flowchart TD
  subgraph Repo["In git, reviewed by pull request"]
    AY["agent.yaml<br/>identity, models, tools, secret NAMES,<br/>budgets, memory"]
    REG["registry.json<br/>logical model to provider,<br/>guardrail profiles"]
    POL["policy/data, rules,<br/>waivers.yaml"]
    CMP["compose.yaml, Terraform<br/>local values and wiring"]
  end
  subgraph Store["In a secret store, never in git"]
    PROV["Provider API keys<br/>Secrets Manager: llm/openai, llm/anthropic"]
    APP["Application secrets<br/>Secrets Manager: crm/api ..."]
  end
  subgraph Env["Environment variables at run time"]
    EP["Endpoints: ENT_MODEL_GATEWAY_URL,<br/>OTEL_EXPORTER_OTLP_ENDPOINT,<br/>AWS_ENDPOINT_URL (local only)"]
    GK["Agent gateway key:<br/>ENT_MODEL_GATEWAY_KEY"]
    SW["Switches: ROUTER_MODE, LOG_LEVEL ..."]
  end
  AY -->|"read at start-up"| AGENT["Agent"]
  APP -->|"secrets.get() at run time"| AGENT
  GK --> AGENT
  EP --> AGENT
  REG -->|"read at start-up"| ROUTER["Model router"]
  PROV -->|"read per request, cached 300 s"| ROUTER
  SW --> ROUTER
  POL --> CI["CI pipeline"]
```

Rules of thumb:

- A **secret value** is only ever in Secrets Manager (Floci locally). Code and `agent.yaml` hold its *name*.
- **Identity and policy choices** (who the agent is, which models and tools it may use) are in `agent.yaml`, so reviewers and policy see them.
- **Where things are** (endpoints, mode) are environment variables, so the same image runs locally and in AWS.
- **Approved choices** (model registry, allow-lists, rules) are repository files changed through pull requests.
- In AWS, credentials are an IAM role; the dummy `test` keys and `AWS_ENDPOINT_URL` exist only on a developer machine.
- The provider keys used for testing were pasted into a chat session on 2026-10-06. Treat them as exposed and rotate them.

Next: workbook sheets **Config and Secrets Storage** and **Env Vars**.

## 6. Try it yourself (30 minutes)

1. `.\harness\dev.ps1 up -Mode replay`, then open Jaeger (http://localhost:16686) and Floci UI (http://localhost:4500).
2. Create an agent and run it (`instructions.txt`, section 7), then call it with `.\harness\call-agent.ps1 -Port 8091 -Prompt "Say hello in exactly three words."`. The script prints the trace ID and the call tree; open that trace in Jaeger. You should see `invoke_agent`, a `chat` span, and the `router chat` span.
3. In your agent's folder run `uv run ent-agent check .`, then break a rule on purpose (add `import openai` and `client = openai.OpenAI()` to `agent.py`) and read the finding with `uv run ent-agent rules ENT-002`.
4. Add a tool with `side_effects=True`, call the agent, and resume it from the 202 response with `{"resume": {"approved": true}}`.
5. Open `harness/model-router/registry.json`, read how `chat-default` falls back from Anthropic to OpenAI, then find where the router builds that into a Portkey config (`registry.portkey_config` in `harness/model-router/src/model_router/app.py`).

## 7. Keeping the learning material current

| Artifact | Source of truth | Rebuild |
|---|---|---|
| Workbook | docs/requirements-traceability.md, the code (read automatically), `docs/learning/descriptions/*.json`, `scripts/learning_content.py` | `python scripts/build_learning_workbook.py` (needs `openpyxl`; close the file in Excel first) |
| Rules page | `rules_catalog.py` | a test fails if the generated page is stale |
| This guide | hand-written | edit it when the flow changes |

New or renamed functions show up in the workbook's Code Reference automatically, with the description "(no description yet)" until one is added to
`docs/learning/descriptions/`. The build fails if a path or environment variable it documents no longer exists.
