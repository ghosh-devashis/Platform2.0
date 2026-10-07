"""Measure what the SDK adds to an invocation, and how fast the policy checks are (NFR-01).

    uv run python scripts/bench_overhead.py [--requests 500] [--check]

- SDK overhead: the same trivial one-node graph called directly vs through `EnterpriseAgentApp` over its ASGI interface
  (HTTP parsing, guardrails on input and output, tracing, audit context, session handling, JSON). Model and tool time
  are excluded by construction. Target: p95 overhead under 20 ms.
- Policy checks: `lint.check_source` on a 1,000-line file and `check_paths` over a freshly generated agent project. Target: under 2 s.

`--check` exits 1 if a target is missed (used in CI). Numbers depend on the machine; CI runners are slower than a laptop.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import tempfile
import time

import httpx2
from langchain_core.messages import AIMessage
from langgraph.graph import START, MessagesState, StateGraph
from opentelemetry.sdk.trace import TracerProvider

from ent_agent_sdk import EnterpriseAgentApp
from ent_agent_sdk.devtools import lint, scaffold

OVERHEAD_TARGET_MS = 20.0
LINT_TARGET_S = 2.0


def graph():
    builder = StateGraph(MessagesState)
    builder.add_node("answer", lambda state: {"messages": [AIMessage(content="ok")]})
    builder.add_edge(START, "answer")
    return builder.compile()


def percentile(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(pct / 100 * (len(ordered) - 1))))]


def summarize(values_ms: list[float]) -> dict[str, float]:
    return {"p50": round(percentile(values_ms, 50), 2), "p95": round(percentile(values_ms, 95), 2),
            "p99": round(percentile(values_ms, 99), 2), "mean": round(statistics.fmean(values_ms), 2)}


async def measure(requests: int) -> dict[str, dict[str, float]]:
    bare_graph = graph()
    results: dict[str, dict[str, float]] = {}

    async def time_calls(call) -> list[float]:  # noqa: ANN001
        for _ in range(50):  # warm up
            await call()
        samples = []
        for _ in range(requests):
            started = time.perf_counter()
            await call()
            samples.append((time.perf_counter() - started) * 1000)
        return samples

    results["bare graph"] = summarize(await time_calls(lambda: bare_graph.ainvoke({"messages": [("user", "hi")]})))
    for label, kwargs in (("SDK, stateless (checkpointer=None)", {"checkpointer": None}), ("SDK, default (session memory on)", {})):
        app = EnterpriseAgentApp(graph(), name="bench", team="t", tracer_provider=TracerProvider(), **kwargs)
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app.asgi), base_url="http://bench") as client:
            results[label] = summarize(await time_calls(lambda c=client: c.post("/invocations", json={"prompt": "hi"})))
    return results


def lint_timings() -> dict[str, float]:
    source = "\n".join(
        f"def function_{i}(a, b):\n    value = a + b\n    return str(value)\n" for i in range(250)
    )
    started = time.perf_counter()
    lint.check_source(source, "big.py")
    single = time.perf_counter() - started
    with tempfile.TemporaryDirectory() as tmp:  # a real project laid out the way `ent-agent new` makes it
        scaffold.create_project("bench-agent", team="platform", directory=tmp)
        started = time.perf_counter()
        lint.check_paths([f"{tmp}/bench-agent"])
        tree = time.perf_counter() - started
    return {"lines": source.count("\n"), "check_source_seconds": round(single, 4), "check_project_seconds": round(tree, 4)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--requests", type=int, default=500)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    latency = asyncio.run(measure(args.requests))
    lints = lint_timings()
    bare = latency["bare graph"]
    overhead = {label: round(stats["p95"] - bare["p95"], 2) for label, stats in latency.items() if label != "bare graph"}
    report = {"requests": args.requests, "latency_ms": latency, "sdk_overhead_p95_ms": overhead, "policy_checks": lints,
              "targets": {"sdk_overhead_p95_ms": OVERHEAD_TARGET_MS, "policy_check_seconds": LINT_TARGET_S}}
    print(json.dumps(report, indent=2))
    ok = all(v < OVERHEAD_TARGET_MS for v in overhead.values()) and max(lints["check_source_seconds"], lints["check_project_seconds"]) < LINT_TARGET_S
    print("TARGETS MET" if ok else "TARGETS MISSED")
    return 1 if (args.check and not ok) else 0


if __name__ == "__main__":
    sys.exit(main())
