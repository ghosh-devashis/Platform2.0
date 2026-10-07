import assert from "node:assert/strict";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { AIMessage } from "@langchain/core/messages";
import { EnterpriseAgentApp, Guardrails } from "../src/index.js";
import { SESSION_HEADER, TRACE_ID_HEADER, USER_HEADER, defaultOutputMapper } from "../src/app.js";
import { captureAudit, echo, failing, graphOf, memoryProvider, post, start } from "./helpers.js";

test("ping reports Healthy", async () => {
  const r = await start(echo);
  try {
    const res = await fetch(`${r.url}/ping`);
    const body: any = await res.json();
    assert.equal(res.status, 200);
    assert.equal(body.status, "Healthy");
    assert.equal(typeof body.time_of_last_update, "number");
  } finally {
    await r.close();
  }
});

test("standard prompt returns output, no session_id without header", async () => {
  const r = await start(echo);
  try {
    const res = await post(r.url, { prompt: "hello" });
    assert.equal(res.status, 200);
    assert.deepEqual(res.json, { output: "echo: hello" });
    assert.match(res.headers.get(TRACE_ID_HEADER)!, /^[0-9a-f]{32}$/);
  } finally {
    await r.close();
  }
});

test("session header is echoed and becomes the LangGraph thread_id", async () => {
  let seen: string | undefined;
  const r = await start((_state, config) => {
    seen = config?.configurable?.thread_id;
    return { messages: [new AIMessage("ok")] };
  });
  try {
    const res = await post(r.url, { prompt: "hi" }, { [SESSION_HEADER]: "abc-123" });
    assert.equal(res.json.session_id, "abc-123");
    assert.equal(seen, "abc-123");
    await post(r.url, { prompt: "hi" });
    assert.equal(seen, "default");
  } finally {
    await r.close();
  }
});

test("invalid JSON and non-object bodies are rejected with 400 InvalidRequest", async () => {
  const r = await start(echo);
  try {
    const bad = await post(r.url, "not json");
    assert.equal(bad.status, 400);
    assert.deepEqual(bad.json, { error: { type: "InvalidRequest", message: "Request body must be valid JSON." } });
    const arr = await post(r.url, ["a", "list"]);
    assert.equal(arr.status, 400);
    assert.equal(arr.json.error.message, "Request body must be a JSON object.");
    assert.equal(bad.headers.get(TRACE_ID_HEADER), null);
  } finally {
    await r.close();
  }
});

test("empty body is treated as {}", async () => {
  const r = await start(() => ({ messages: [new AIMessage("x")] }), { inputMapper: () => ({ messages: [] }) });
  try {
    const res = await post(r.url, "");
    assert.equal(res.status, 200);
  } finally {
    await r.close();
  }
});

test("agent error returns generic 500 without leaking details; span has the error type only", async () => {
  const r = await start(failing);
  try {
    const res = await post(r.url, { prompt: "hi" });
    assert.equal(res.status, 500);
    assert.equal(res.json.error.type, "AgentError");
    assert.ok(!JSON.stringify(res.json).includes("boom"));
    const root = r.exporter.getFinishedSpans().find((s) => s.name === "invoke_agent test-agent")!;
    assert.equal(root.attributes["ent.outcome"], "error");
    assert.equal(root.attributes["error.type"], "RangeError");
    assert.ok(!JSON.stringify(root.status).includes("boom"));
  } finally {
    await r.close();
  }
});

test("guardrails: input blocked 400, output withheld 502 (strict profile)", async () => {
  const cap = captureAudit();
  const r = await start(echo, { guardrails: new Guardrails("strict") });
  try {
    const blocked = await post(r.url, { prompt: "mail me at a@b.com" });
    assert.equal(blocked.status, 400);
    assert.equal(blocked.json.error.type, "GuardrailBlocked");
    assert.ok(blocked.headers.get(TRACE_ID_HEADER));
    assert.equal(cap.events[0].event_type, "guardrail_block");
    assert.equal(cap.events[0].agent, "test-agent");
    assert.ok(!JSON.stringify(cap.events).includes("a@b.com"));
  } finally {
    await r.close();
  }
  const r2 = await start(() => ({ messages: [new AIMessage("contact a@b.com")] }), { guardrails: new Guardrails("strict") });
  try {
    const res = await post(r2.url, { prompt: "hi" });
    assert.equal(res.status, 502);
    assert.equal(res.json.error.message, "Response withheld by guardrail policy.");
  } finally {
    await r2.close();
    cap.stop();
  }
});

test("standard profile redacts PII in the output and carries user id into audit", async () => {
  const cap = captureAudit();
  const r = await start(echo);
  try {
    const res = await post(r.url, { prompt: "my email is a@b.com" }, { [USER_HEADER]: "u-1", [SESSION_HEADER]: "s-1" });
    assert.equal(res.json.output, "echo: my email is [REDACTED:pii]");
    const e = cap.events.find((x) => x.event_type === "guardrail_redact")!;
    assert.equal(e.user_id, "u-1");
    assert.equal(e.session_id, "s-1");
    assert.equal(e.trace_id, res.headers.get(TRACE_ID_HEADER));
  } finally {
    await r.close();
    cap.stop();
  }
});

test("health check: false or throwing gives 503 Unhealthy; busy while a request runs", async () => {
  const bad = await start(echo, { healthCheck: () => false });
  const boom = await start(echo, {
    healthCheck: () => {
      throw new Error("x");
    },
  });
  try {
    for (const r of [bad, boom]) {
      const res = await fetch(`${r.url}/ping`);
      assert.equal(res.status, 503);
      assert.equal(((await res.json()) as any).status, "Unhealthy");
    }
  } finally {
    await bad.close();
    await boom.close();
  }
  let release!: () => void;
  const gate = new Promise<void>((r) => (release = r));
  const busy = await start(async () => {
    await gate;
    return { messages: [new AIMessage("done")] };
  });
  try {
    const pending = post(busy.url, { prompt: "hi" });
    await new Promise((r) => setTimeout(r, 100));
    assert.equal(((await (await fetch(`${busy.url}/ping`)).json()) as any).status, "HealthyBusy");
    release();
    await pending;
    assert.equal(((await (await fetch(`${busy.url}/ping`)).json()) as any).status, "Healthy");
  } finally {
    await busy.close();
  }
});

test("W3C traceparent is continued; trace id header matches", async () => {
  const r = await start(echo);
  try {
    const traceId = "4bf92f3577b34da6a3ce929d0e0e4736";
    const res = await post(r.url, { prompt: "hi" }, { traceparent: `00-${traceId}-00f067aa0ba902b7-01` });
    assert.equal(res.headers.get(TRACE_ID_HEADER), traceId);
    const root = r.exporter.getFinishedSpans().find((s) => s.name.startsWith("invoke_agent"))!;
    assert.equal(root.spanContext().traceId, traceId);
    assert.equal(root.parentSpanContext?.spanId, "00f067aa0ba902b7");
  } finally {
    await r.close();
  }
});

test("input/output mappers, unknown routes and methods", async () => {
  const r = await start(() => ({ messages: [new AIMessage("ignored")] }), {
    inputMapper: () => ({ messages: [{ role: "user", content: "x" }] }),
    outputMapper: () => ({ answer: 42 }),
  });
  try {
    assert.deepEqual((await post(r.url, { q: "x" })).json, { answer: 42 });
    assert.equal((await fetch(`${r.url}/nope`)).status, 404);
    const m = await fetch(`${r.url}/invocations`);
    assert.equal(m.status, 405);
    assert.equal(m.headers.get("allow"), "POST");
  } finally {
    await r.close();
  }
});

test("defaultOutputMapper handles non-message states", () => {
  assert.deepEqual(defaultOutputMapper({ a: 1, b: [new AIMessage("hi")] }), { a: 1, b: [{ type: "ai", content: "hi" }] });
  assert.deepEqual(defaultOutputMapper("text"), { output: "text" });
});

test("fromManifest takes name, team, classification and guardrail profile", async () => {
  const dir = mkdtempSync(join(tmpdir(), "ent-"));
  const file = join(dir, "agent.yaml");
  writeFileSync(file, "name: crm-agent\nteam: sales\ndata_classification: restricted\nguardrail_profile: strict\n");
  const { provider, exporter } = memoryProvider();
  const app = EnterpriseAgentApp.fromManifest(graphOf(echo), file, { version: "2.0.0", tracerProvider: provider });
  assert.equal(app.name, "crm-agent");
  assert.equal(app.team, "sales");
  assert.equal(app.guardrails.profile, "strict");
  const server = await app.listen(0, "127.0.0.1");
  const port = (server.address() as any).port;
  try {
    await post(`http://127.0.0.1:${port}`, { prompt: "hi" });
    const root = exporter.getFinishedSpans().find((s) => s.name.startsWith("invoke_agent"))!;
    assert.equal(root.attributes["ent.data_classification"], "restricted");
    assert.equal(root.attributes["ent.team"], "sales");
  } finally {
    await app.shutdown();
  }
});

test("Guardrails.off emits a waiver_in_effect audit event at startup", () => {
  const cap = captureAudit();
  try {
    const { provider } = memoryProvider();
    new EnterpriseAgentApp(graphOf(echo), { name: "w", guardrails: Guardrails.off({ waiver: "WAIVER-1" }), tracerProvider: provider });
    assert.equal(cap.events[0].event_type, "waiver_in_effect");
    assert.equal(cap.events[0].details.waiver, "WAIVER-1");
  } finally {
    cap.stop();
  }
});

test("graceful shutdown waits for in-flight requests and flushes spans", async () => {
  let release!: () => void;
  const gate = new Promise<void>((r) => (release = r));
  const r = await start(async () => {
    await gate;
    return { messages: [new AIMessage("late")] };
  });
  const pending = post(r.url, { prompt: "hi" });
  await new Promise((x) => setTimeout(x, 100));
  const done = r.app.shutdown();
  setTimeout(release, 150);
  const res = await pending;
  await done;
  assert.equal(res.status, 200);
  assert.equal(res.json.output, "late");
  await assert.rejects(fetch(`${r.url}/ping`));
});

test("shutdown timeout abandons stuck requests", async () => {
  const r = await start(() => new Promise(() => {}), { shutdownTimeoutMs: 200 });
  const pending = post(r.url, { prompt: "hi" }).catch(() => "dropped");
  await new Promise((x) => setTimeout(x, 100));
  const t0 = Date.now();
  await r.app.shutdown();
  assert.ok(Date.now() - t0 < 3000);
  assert.equal(await pending, "dropped");
});
