# Model router

The local stand-in for Portkey's enterprise control plane. Agents send OpenAI-style requests with a **logical model name**
(`chat-default`, `chat-fast`); the router turns that into a Portkey config (provider, key, fallbacks, retries, guardrails)
and calls the open-source Portkey gateway. Agents never hold a provider key.

```
agent ──(model: "chat-default", Authorization: Bearer <agent's gateway key>)──▶ router :8788 ──▶ Portkey :8787 ──▶ OpenAI / Anthropic
```

With Portkey enterprise (saved configs, virtual keys, native budgets/caching/HA) this router goes away; agent code doesn't change.

## What it does for every request

| Step | What | Setting |
|---|---|---|
| Authenticate | Each agent/team has its own gateway credential; unknown callers get 401 | `ROUTER_GATEWAY_KEYS=my-agent=key1,tests=key2` (unset = open, local dev only) |
| Check the model | Only names in `registry.json` are accepted (400 `model_not_approved`) | `ROUTER_REGISTRY` |
| Limits | Requests per minute and tokens per UTC day, per client (429 with `Retry-After`) | `ROUTER_RATE_LIMIT_RPM`, `ROUTER_TOKEN_BUDGET_PER_DAY` (0 = off) |
| Guardrails | The caller's profile (`x-ent-guardrail-profile`, set by the SDK) adds Portkey before/after hooks: `standard` blocks credentials, `strict` also blocks personal data. A block returns 400/502 `guardrail_blocked` | `guardrail_profiles` in `registry.json` |
| Cache | Identical requests to models with `cache` set, from `public`/`internal` agents, are served from memory | `cache` per model in `registry.json` |
| Route | Portkey config with provider keys read from Secrets Manager (`llm/anthropic`, `llm/openai`) | `AWS_ENDPOINT_URL` (Floci locally) |
| Fail over | Several gateway instances are used round-robin; one that refuses connections is skipped for 10 s. Never retried after the request may have reached a gateway; never falls back to a provider | `PORTKEY_URLS=a,b` (else `PORTKEY_URL`) |
| Observe | A span per call (`router chat <model>`) joined to the agent's trace, with client, profile, mode, status and token usage; no prompt content | standard `OTEL_*` settings |

## Modes (`ROUTER_MODE`)

| Mode | Behaviour |
|---|---|
| `real` (default) | Real providers through Portkey |
| `record` | Like real, and saves each answer under `recordings/` (committed as test fixtures) |
| `replay` | Answers from recordings only: no keys, no gateway, no internet. No recording: a marked stub (`ROUTER_REPLAY_MISSING=stub`) or a 404 (`error`, used in CI) |
| `local` | A local model (e.g. Ollama) through Portkey, from each model's `local` block |

## Run

```powershell
.\harness\dev.ps1 up -Mode replay        # in Docker, with everything else
.\harness\model-router\run-local.ps1     # or on the host (needs Floci for secrets in real mode)
```

## Honest limits

In-memory state (rate counters, budgets, cache) is per router process and lost on restart: right for local/CI and small
deployments, not a replacement for Portkey enterprise's shared budgets. Token budgets count non-streaming answers (providers
report usage there). The cache is exact-match only (semantic caching is an enterprise Portkey feature). Gateway guardrails use
Portkey's built-in regex check, so they are a safety net next to the SDK's guardrails and Bedrock Guardrails, not a classifier.

## Provider quirks handled by the router

- Callers' `max_tokens` becomes `max_completion_tokens` (accepted by both providers). If none is given, the model's `default_max_completion_tokens` is used (Anthropic requires one).
- A target's `params` in `registry.json` are merged into the call (e.g. `reasoning_effort: none` on the OpenAI fallback so function tools work).
- Portkey's `hook_results` field echoes prompt excerpts, so it is removed from replies and recordings.
