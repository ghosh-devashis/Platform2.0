# __NAME__

Agent owned by **__TEAM__** (data classification: __CLASSIFICATION__), built on the Enterprise Agent SDK.

## Run it

```bash
uv sync
uv run pytest                      # tests use a scripted fake model: no keys or network needed
```

Against the local platform stack (Floci, Jaeger, Portkey, routers; needs Docker):

```bash
export PLATFORM_DIR=../Platform2.0          # your checkout of the platform repo
docker compose --profile app up -d --build
uv run ent-agent dev seed                   # secrets + AgentCore runtime from agent.yaml
curl -X POST http://localhost:8080/invocations -H 'content-type: application/json' -d '{"prompt":"hi"}'
```

Traces: http://localhost:16686 (Jaeger). With no provider keys the model router answers from recordings or a
clearly marked stub (ROUTER_MODE=replay); see the platform docs to record real answers.

## What's here

| File | Purpose |
|------|---------|
| `agent.yaml` | The manifest: owner, data classification, models, tools, secrets, guardrail profile |
| `src/__PACKAGE__/agent.py` | The graph, tools and app |
| `waivers.yaml` | Approved exceptions (empty is good) |
| `.github/workflows/ci.yml` | Calls the platform's pipeline; do not remove or override its gates |
| `CLAUDE.md`, `AGENTS.md`, `.github/copilot-instructions.md` | The standards, for AI coding assistants |
