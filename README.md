# Platform 2.0 — Enterprise Agent Platform

Standardized agentic development: one Enterprise Agent SDK (LangGraph/LangChain + observability, security,
secrets), enforced by CI/CD, testable locally against an AgentCore-compatible runtime and Floci (AWS look-alike),
with a VS Code extension for developers and their AI code assistants.

- Requirements: [docs/requirements-spec.md](docs/requirements-spec.md)
- **What is built vs. asked, requirement by requirement:** [docs/requirements-traceability.md](docs/requirements-traceability.md)
- Build plan and progress: [docs/build-plan.md](docs/build-plan.md)
- Session log (history and hand-off between Claude sessions): [docs/session-log.md](docs/session-log.md)
- Local AWS (Floci) setup: [docs/floci-setup.md](docs/floci-setup.md)

## Repository layout

| Folder | What lives there |
|--------|------------------|
| `sdk/` | `ent-agent-sdk` — the Enterprise Agent SDK (Python) |
| `harness/` | Local runtime pieces: invoke router, OTel collector config, seed scripts |
| `policy/` | Policy bundle: Semgrep rules, OPA/Conftest policies, dependency allowlist, pre-commit config |
| `templates/` | Project template used to scaffold new agent repos |
| `vscode-extension/` | VS Code extension + MCP standards server |
| `docs/` | Specs, guides, build plan |
| `compose.yaml` | Local stack: Floci (:4566), Floci UI (:4500), Portkey LLM gateway (:8787) |

Agents are not kept in this repository. Create each one in its own folder next to it, for example
`uv run ent-agent new my-agent --team learning --dir ..`, and test it there.

## Quick start (local AWS)

```bash
docker compose up -d
```

Floci: http://localhost:4566 · Floci UI: http://localhost:4500 · Portkey gateway: http://localhost:8787 (console `/public/`)
