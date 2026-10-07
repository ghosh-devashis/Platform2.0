# Onboarding: from zero to a running, tested agent (one page)

Goal (spec G3): under 30 minutes. You need Docker Desktop, `uv` and Git. Windows steps shown; macOS/Linux use `harness/dev.sh`.

## 1. Start the local platform (about 5 minutes the first time)

```powershell
git clone <platform repo> Platform2.0 ; cd Platform2.0
uv sync
.\harness\dev.ps1 up -Mode replay        # Floci (fake AWS), Portkey, Jaeger and the two routers; no API keys needed
```

Open http://localhost:16686 (traces) and http://localhost:4500 (Floci console).

## 2. Create your agent (1 minute)

```powershell
uv run ent-agent new crm-agent --team sales --classification internal --dir ..
cd ..\crm-agent ; uv sync ; uv run pytest        # the generated test uses a scripted fake model
```

You get the code, `agent.yaml` (who owns it, what it may use), tests, Dockerfile, CI, pre-commit, golden questions for the
evaluation gate, and instruction files for AI assistants.

## 3. Run it against the local stack

```powershell
$env:PLATFORM_DIR = "..\Platform2.0"
docker compose --profile app up -d --build
uv run ent-agent dev seed                         # secrets, tables and the AgentCore runtime from agent.yaml
Invoke-RestMethod -Method Post -Uri http://localhost:8080/invocations -ContentType 'application/json' -Body '{"prompt":"hi"}'
```

Without provider keys, answers come from the router's replay mode (a clearly marked stub until you record real answers).
Every call is a trace in Jaeger: agent → graph step → model call → tool call.

## 4. Write code the compliant way

| To... | Use |
|-------|-----|
| call a model | `get_model()` (never a provider client): the model is the first entry under `models:` in `agent.yaml`, so it is never named in code |
| read a secret | `secrets.get("name")` |
| add a tool | `@tools.tool(owner=..., data_classification=...)`; `side_effects=True` pauses for human approval |
| keep a conversation | send the session header; set `memory: {backend: dynamodb}` in `agent.yaml` to persist it |
| stay in budget | `token_budget: {soft: ..., hard: ...}` in `agent.yaml` |

Check as you go: `uv run ent-agent check` (the VS Code extension shows the same findings live, with one-click fixes).
Ask `ent-agent rules ENT-001` to explain any rule. Your AI assistant can use the standards through `ent-agent mcp`.

## 5. Ship

Commit; pre-commit runs the fast policy checks. Open a pull request: the platform pipeline runs the policy gates, your tests
against the local runtime, the evaluation gate, vulnerability/SBOM/licence checks, and builds the image. Fix what it names
(each failure has a rule ID and a fix). Need an exception? Add a time-boxed waiver to `waivers.yaml` (see [rules](../policy/docs/rules.md)).

## Where to go next

[SDK API summary](../sdk/src/ent_agent_sdk/docs/api.md) · [rules](../policy/docs/rules.md) · [support](support.md) ·
[compatibility matrix](compatibility-matrix.md) · [requirements → code map](requirements-traceability.md)
