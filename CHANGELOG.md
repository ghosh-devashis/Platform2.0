# Changelog

All notable changes per release, newest first. The SDK, policy bundle, template and extension are versioned
independently (semantic versioning); [docs/compatibility-matrix.md](docs/compatibility-matrix.md) says which
versions belong together. A release also updates `policy/data/versions.json` (supported series and sunset dates).

## Unreleased

**SDK (Python) and template**
- `get_model()` with no name returns the first model under `models:` in `agent.yaml` (manifest found through
  `AGENT_MANIFEST`, else `./agent.yaml`); new `declared_models()` lists them in order. The template's `agent.py` no longer
  names a model, so the model is chosen in one place. Explicit names (`get_model("chat-fast")`) still work.
- The ENT-001 quick fix now inserts `get_model()`; the template's `CLAUDE.md`/`AGENTS.md`/Copilot instructions teach the
  new form. Existing agents: `ent-agent update` previews the change (their `agent.py` shows as a conflict if edited);
  refresh the SDK (`uv sync --reinstall-package ent-agent-sdk`) before changing `agent.py` to `get_model()`.
- VS Code extension (source only; repackage to ship): snippets `ent-node` / `ent-model` use `get_model()`.

**Model router**
- Each router trace span now carries `gen_ai.response.model` (the real model that answered, including after a fallback).

## 0.1.0 (first release)

**SDK (Python)**
- `EnterpriseAgentApp` on the AgentCore contract; automatic OpenTelemetry tracing and JSON logs; secrets from
  Secrets Manager / SSM; models by logical name through the gateway; guardrails (layer 2), audit stream, tool registry
  with human approval for side-effect tools, token budgets, session memory (in-memory and DynamoDB), identity
  (AgentCore Identity, STS), build metadata, `agent.yaml` manifest; default gateway health check and clear errors
  when the gateway is down.
- CLI: `ent-agent new / update / check / manifest validate / dev seed / eval run / mcp / rules / metadata`.
- MCP standards server for AI assistants.

**SDK (TypeScript)**: port of the core (app, guardrails, audit, tools, secrets, models, manifest, tracing).

**Local runtime**: Floci, Floci UI, Portkey gateway, Jaeger, model router (real / record / replay / local modes,
gateway guardrails, authentication, rate limits and budgets, cache, failover), invoke router, seeding, one-command
stack (`harness/dev.ps1`, `harness/dev.sh`).

**Policy bundle 0.1.0**: Semgrep, OPA/Conftest, gitleaks, waivers, pipeline meta-check, licences, supported-version
check (rules ENT-001..062), reusable GitHub Actions pipeline with evaluation gate and image signing.

**Template 2**: ready-to-run agent project with Docker, CI, pre-commit, golden questions, MCP config and AI
instruction files; `ent-agent update` pulls template changes in as a reviewable diff.

**VS Code extension 0.1.0**: new agent, live diagnostics and quick fixes, runtime controls, invoke panel, MCP
registration, snippets and code lenses, upgrade assistant, update feed, opt-in telemetry.

**Deployment rehearsal**: Terraform (`deploy/`) applies to Floci or AWS (`target = local | aws`).
