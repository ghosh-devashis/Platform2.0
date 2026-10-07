import assert from "node:assert/strict";
import { test } from "node:test";
import { FakeListChatModel } from "@langchain/core/utils/testing";
import { AIMessage } from "@langchain/core/messages";
import { z } from "zod";
import { ToolRegistry } from "../src/tools.js";
import * as obs from "../src/observability.js";
import { buildTracerProvider } from "../src/observability.js";
import { formatLog } from "../src/log.js";
import { captureAudit, post, start } from "./helpers.js";

test("span tree: invoke_agent > node > (chat, execute_tool) with GenAI attributes and no content", async () => {
  const cap = captureAudit();
  const reg = new ToolRegistry();
  const tool = reg.defineTool({
    name: "lookup", description: "d", schema: z.object({ id: z.string() }), owner: "crm-team",
    dataClassification: "confidential", sideEffects: true, func: () => "result-content",
  });
  const model = new FakeListChatModel({ responses: ["model-response-content"] });
  const r = await start(
    async () => {
      await model.invoke("prompt-content");
      await tool.invoke({ id: "42" });
      return { messages: [new AIMessage("final")] };
    },
    { team: "sales", dataClassification: "internal" },
  );
  try {
    const res = await post(r.url, { prompt: "prompt-content" }, { "x-amzn-bedrock-agentcore-runtime-session-id": "sess-1" });
    assert.equal(res.status, 200);
    const spans = r.exporter.getFinishedSpans();
    const byName = (n: string) => spans.find((s) => s.name === n || s.name.startsWith(n))!;
    const root = byName("invoke_agent test-agent");
    const node = byName("node");
    const chat = byName("chat ");
    const exec = byName("execute_tool lookup");
    for (const s of spans) assert.equal(s.spanContext().traceId, root.spanContext().traceId);
    assert.equal(node.parentSpanContext?.spanId, root.spanContext().spanId);
    assert.equal(chat.parentSpanContext?.spanId, node.spanContext().spanId);
    assert.equal(exec.parentSpanContext?.spanId, node.spanContext().spanId);

    assert.equal(root.attributes[obs.OPERATION], "invoke_agent");
    assert.equal(root.attributes[obs.AGENT_NAME], "test-agent");
    assert.equal(root.attributes[obs.AGENT_VERSION], "1.2.3");
    assert.equal(root.attributes[obs.TEAM], "sales");
    assert.equal(root.attributes[obs.CONVERSATION_ID], "sess-1");
    assert.equal(root.attributes[obs.OUTCOME], "success");
    assert.equal(root.attributes[obs.SPAN_TYPE], "agent");
    assert.equal(node.attributes[obs.SPAN_TYPE], "chain");
    assert.equal(node.attributes[obs.NODE], "node");
    assert.equal(chat.attributes[obs.OPERATION], "chat");
    assert.equal(chat.attributes[obs.SPAN_TYPE], "llm");
    assert.ok(chat.attributes[obs.REQUEST_MODEL]);
    assert.equal(exec.attributes[obs.OPERATION], "execute_tool");
    assert.equal(exec.attributes[obs.TOOL_NAME], "lookup");
    assert.equal(exec.attributes[obs.SPAN_TYPE], "tool");
    assert.equal(exec.attributes[obs.TOOL_OWNER], "crm-team");
    assert.equal(exec.attributes[obs.TOOL_CLASSIFICATION], "confidential");
    assert.equal(exec.attributes[obs.TOOL_SIDE_EFFECTS], true);
    // audit events are attached as span events on the active span, never with content
    const dump = JSON.stringify(spans.map((s) => ({ a: s.attributes, e: s.events, n: s.name, st: s.status })));
    for (const secret of ["prompt-content", "model-response-content", "result-content"]) assert.ok(!dump.includes(secret), secret);
  } finally {
    await r.close();
    cap.stop();
  }
});

test("tool errors mark the tool span with the error type only", async () => {
  const cap = captureAudit();
  const reg = new ToolRegistry();
  const tool = reg.defineTool({
    name: "bad", description: "d", schema: z.object({ id: z.string() }), owner: "o", retries: 0,
    func: () => {
      throw new RangeError("sensitive detail");
    },
  });
  const r = await start(async () => {
    await tool.invoke({ id: "1" });
    return { messages: [new AIMessage("x")] };
  });
  try {
    const res = await post(r.url, { prompt: "hi" });
    assert.equal(res.status, 500);
    const exec = r.exporter.getFinishedSpans().find((s) => s.name === "execute_tool bad")!;
    assert.equal(exec.attributes[obs.ERROR_TYPE], "ToolError");
    assert.ok(!JSON.stringify([exec.attributes, exec.events, exec.status, exec.name]).includes("sensitive"));
  } finally {
    await r.close();
    cap.stop();
  }
});

test("exporter selection comes only from OTEL_* settings", async () => {
  const saved = { ...process.env };
  try {
    delete process.env.OTEL_EXPORTER_OTLP_ENDPOINT;
    delete process.env.OTEL_EXPORTER_OTLP_TRACES_ENDPOINT;
    delete process.env.OTEL_SDK_DISABLED;
    const processors = (p: any) => p._activeSpanProcessor?._spanProcessors ?? [];
    const none = buildTracerProvider({ serviceName: "a", serviceVersion: "1" });
    assert.equal(processors(none).length, 0);
    process.env.OTEL_EXPORTER_OTLP_ENDPOINT = "http://localhost:4318";
    const some = buildTracerProvider({ serviceName: "a", serviceVersion: "1" });
    assert.equal(processors(some).length, 1);
    await some.shutdown().catch(() => {});
    process.env.OTEL_SDK_DISABLED = "true";
    const off = buildTracerProvider({ serviceName: "a", serviceVersion: "1" });
    assert.equal(processors(off).length, 0);
  } finally {
    for (const k of Object.keys(process.env)) if (!(k in saved)) delete process.env[k];
    Object.assign(process.env, saved);
  }
});

test("resource attributes include service, team, sdk version and OTEL_RESOURCE_ATTRIBUTES", () => {
  process.env.OTEL_RESOURCE_ATTRIBUTES = "deployment.environment=dev";
  try {
    const p: any = buildTracerProvider({ serviceName: "svc", serviceVersion: "9", team: "t", extraAttributes: { "ent.git.sha": "abc" } });
    const a = p._config?.resource?.attributes ?? p["_resource"]?.attributes ?? p.resource?.attributes;
    assert.equal(a["service.name"], "svc");
    assert.equal(a["service.version"], "9");
    assert.equal(a["ent.team"], "t");
    assert.equal(a["ent.git.sha"], "abc");
    assert.equal(a["deployment.environment"], "dev");
    assert.ok(a["ent.sdk.version"]);
  } finally {
    delete process.env.OTEL_RESOURCE_ATTRIBUTES;
  }
});

test("JSON logs carry trace and span ids inside a span", async () => {
  const r = await start(async () => {
    const line = JSON.parse(formatLog("INFO", "x", "hello"));
    assert.match(line.trace_id, /^[0-9a-f]{32}$/);
    assert.match(line.span_id, /^[0-9a-f]{16}$/);
    return { messages: [new AIMessage("x")] };
  });
  try {
    assert.equal((await post(r.url, { prompt: "hi" })).status, 200);
    const outside = JSON.parse(formatLog("ERROR", "x", "m", new Error("e")));
    assert.equal(outside.trace_id, undefined);
    assert.match(outside.exception, /Error: e/);
    assert.deepEqual(Object.keys(outside).slice(0, 4), ["timestamp", "level", "logger", "message"]);
  } finally {
    await r.close();
  }
});
