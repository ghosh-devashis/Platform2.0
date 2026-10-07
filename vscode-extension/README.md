# Enterprise Agent Toolkit

VS Code support for agents built on the Enterprise Agent SDK (`ent-agent-sdk`). Requires VS Code 1.137 or newer and the
`ent-agent` CLI (or `uv`, used as a fallback via `uv run ent-agent`).

## Features

| Feature | How to use |
|---------|-----------|
| New Agent wizard (EXT-01) | Command "Enterprise Agent: New Agent" |
| Live policy diagnostics (EXT-02) | Automatic on open/save of `.py`, `agent.yaml`, `pyproject.toml`; rule IDs link to the rules document. "Check Workspace" checks everything |
| Quick fixes (EXT-03) | Lightbulb on a finding that has a fix (adds the needed import too) |
| Local stack controls (EXT-04) | Status bar item (health of Floci, Jaeger, model router, invoke router, agents); commands to start/stop the stack and open Floci UI / Jaeger |
| Invoke and test panel (EXT-05) | "Enterprise Agent: Invoke and Test Agent": response, status, latency, guardrail result, trace link. Localhost agents only |
| AI assistant integration (EXT-06) | MCP server `ent-agent mcp` registered automatically (VS Code MCP API); "Install MCP Standards Server Config" writes `.vscode/mcp.json`; "Install AI Assistant Instructions" creates `CLAUDE.md`, `AGENTS.md`, `.github/copilot-instructions.md` if absent |
| Snippets and code lenses (EXT-07) | `ent-node`, `ent-tool`, `ent-tool-sideeffect`, `ent-secret`, `ent-model`, `ent-app`; lens above each `@tools.tool(...)` and "Run Enterprise checks" on `agent.py` |
| Upgrade assistant (EXT-08) | Notifies when `ent-agent-sdk` is outdated or near end of support; Upgrade runs `uv lock --upgrade-package ent-agent-sdk` then `uv sync` |
| Updates (EXT-09) | Checks `ent.updateFeedUrl` once a day and offers to install a newer VSIX. See `docs/extension-distribution.md` |
| Telemetry (EXT-10) | Opt-in, anonymous, off by default |

## Settings

| Setting | Default | Purpose |
|---------|---------|---------|
| `ent.agentCommand` | `ent-agent` | CLI command; falls back to `uv run ent-agent` if it cannot start |
| `ent.platformDir` | (auto) | Platform2.0 checkout; default is the first workspace folder with `harness/dev.ps1` |
| `ent.diagnostics.enabled` / `.timeoutMs` | true / 30000 | Live checks |
| `ent.rulesDocUrl` | (local rules.md) | Base URL for rule links |
| `ent.runtime.statusBar.enabled` | true | Poll stack health every 10 s |
| `ent.runtime.*Url`, `ent.runtime.agentUrls` | local defaults | Service addresses |
| `ent.runtime.mode` | replay | Mode for "Start Local Stack" |
| `ent.latestSdkVersion` | (from `policy/data/versions.json`) | Latest approved SDK version |
| `ent.updateFeedUrl` | (none) | Private update feed (http(s) URL or folder) |
| `ent.telemetry.enabled` | false | Opt in to telemetry |
| `ent.telemetry.endpoint` | (none) | Optional URL that receives events |

## Telemetry

Off by default. When enabled it records only: event name from a fixed list, rule IDs (`ENT-nnn`), a count, a timestamp and a random
install id. Never code, file names, paths or prompts. Events go to `telemetry.ndjson` in the extension's global storage and, if
`ent.telemetry.endpoint` is set, are POSTed there.

## Build, test, install

```powershell
npm install
npm test            # compile + unit tests (node --test)
npm run package     # dist/enterprise-agent-toolkit-<version>.vsix
code --install-extension dist\enterprise-agent-toolkit-0.1.0.vsix
```

`npm run sync-standards` refreshes `resources/standards.md` from the SDK's agent template. All logic is in `src/core/` (no
VS Code dependency); `src/*.ts` are thin VS Code adapters.
