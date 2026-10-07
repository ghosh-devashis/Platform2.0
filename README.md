# Platform 2.0 — Enterprise Agent Platform

Standardized agentic development: one Enterprise Agent SDK (LangGraph/LangChain + observability, security,
secrets), enforced by CI/CD, testable locally against an AgentCore-compatible runtime and Floci (AWS look-alike),
with a VS Code extension for developers and their AI code assistants.

- **Setting up on your machine: read [instructions.txt](instructions.txt) first** (prerequisites, settings to change, starting the stack, first agent).
- Requirements: [docs/requirements-spec.md](docs/requirements-spec.md)
- **What is built vs. asked, requirement by requirement:** [docs/requirements-traceability.md](docs/requirements-traceability.md)
- Build plan and progress: [docs/build-plan.md](docs/build-plan.md)
- Local AWS (Floci) setup: [docs/floci-setup.md](docs/floci-setup.md)

## Repository layout

| Folder | What lives there |
|--------|------------------|
| `sdk/` | `ent-agent-sdk` — the Enterprise Agent SDK (Python) |
| `harness/` | Local runtime pieces: invoke router, OTel collector config, seed scripts |
| `policy/` | Policy bundle: Semgrep rules, OPA/Conftest policies, dependency allowlist, pre-commit config |
| `vscode-extension/` | VS Code extension + MCP standards server |
| `docs/` | Specs, guides, build plan |
| `compose.yaml` | Local stack: Floci (:4566), Floci UI (:4500), Portkey (:8787), Jaeger (:16686), model router (:8788), invoke router (:4567) |

Agents are not kept in this repository. Create each one in its own folder next to it, for example
`uv run ent-agent new my-agent --team learning --dir ..`, and test it there.

## Quick start (local stack)

Follow [instructions.txt](instructions.txt) for the one-time settings. Then, from the repo root (PowerShell):

```powershell
.\harness\dev.ps1 up -Mode replay   # replay = no provider keys needed
```

Floci: http://localhost:4566 · Floci UI: http://localhost:4500 · Portkey: http://localhost:8787 (console `/public/`) ·
Jaeger: http://localhost:16686 · model router: http://localhost:8788 · invoke router: http://localhost:4567
