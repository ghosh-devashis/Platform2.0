# Performance (NFR-01)

Targets from the spec: SDK overhead under **20 ms p95** per invocation (excluding model and tool time); IDE policy checks
under **2 s**; full CI policy stage under 5 minutes.

## Method

`scripts/bench_overhead.py` calls the same one-node graph (a) directly and (b) through `EnterpriseAgentApp` over its ASGI
interface, 500 requests after a 50-request warm-up. The difference is what the SDK adds: HTTP parsing, input and output
guardrails, tracing, the audit/invocation context, session handling and JSON. It also times the policy checker.
Run it yourself: `uv run python scripts/bench_overhead.py --check` (CI runs it with `--check`, which fails if a target is missed).

## Results (2026-10-06, Windows 11, AMD Ryzen 5 7520U, Python 3.13)

| | p50 | p95 | p99 | mean |
|---|---:|---:|---:|---:|
| Bare graph | 1.71 ms | 2.37 ms | 2.77 ms | 1.77 ms |
| SDK, stateless (`checkpointer=None`) | 3.20 ms | 5.29 ms | 7.12 ms | 3.47 ms |
| SDK, default (session memory on) | 3.57 ms | 5.92 ms | 8.88 ms | 3.79 ms |

**SDK overhead at p95: 2.9 ms stateless, 3.6 ms with session memory: about one sixth of the 20 ms budget.**

| Policy check | Result | Target |
|---|---:|---:|
| `check_source` on a 1,000-line file | 15 ms | 2 s |
| `check_paths` over a freshly generated agent project | 14 ms (measured earlier, over the since-removed example agents) | 2 s |

## What this does not include

- The network hop to the gateway, the model itself and tool calls (by design: "excluding LLM/tool time").
- OTLP export: spans are exported in the background by a batch processor; the benchmark uses no exporter. With an exporter the
  request path only pays for queueing the span.
- DynamoDB-backed session memory adds the DynamoDB round trips (`memory.dynamodb`), typically a few milliseconds each in AWS.
- The full CI policy stage (Semgrep, Conftest, gitleaks) has not been timed: those tools are not installed locally yet.
- CI runners are slower than this laptop; the 20 ms target leaves a wide margin for that.
