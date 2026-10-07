import assert from "node:assert/strict";
import { test } from "node:test";
import { AIMessage } from "@langchain/core/messages";
import { z } from "zod";
import { ModelConfigError, correlationHeaders, getModel } from "../src/models.js";
import { ToolRegistry } from "../src/tools.js";
import { memoryProvider, post, start } from "./helpers.js";

const completion = {
  id: "c1", object: "chat.completion", created: 1, model: "chat-default",
  choices: [{ index: 0, finish_reason: "stop", message: { role: "assistant", content: "hi there" } }],
  usage: { prompt_tokens: 3, completion_tokens: 2, total_tokens: 5 },
};

async function withStubbedFetch<T>(fn: (seen: { url: string; headers: Headers }[]) => Promise<T>): Promise<T> {
  const seen: { url: string; headers: Headers }[] = [];
  const original = globalThis.fetch;
  globalThis.fetch = (async (input: any, init: any) => {
    const url = typeof input === "string" ? input : (input.url ?? String(input));
    if (url.startsWith("http://127.0.0.1")) return original(input, init); // calls to the agent under test
    seen.push({ url, headers: new Headers(init?.headers ?? input.headers) });
    return new Response(JSON.stringify(completion), { status: 200, headers: { "content-type": "application/json" } });
  }) as typeof fetch;
  try {
    return await fn(seen);
  } finally {
    globalThis.fetch = original;
  }
}

test("getModel targets the gateway with its key, no retries, and per-request correlation headers", async () => {
  process.env.ENT_MODEL_GATEWAY_URL = "http://gateway.test/v1";
  process.env.ENT_MODEL_GATEWAY_KEY = "gw-key";
  process.env.OTEL_RESOURCE_ATTRIBUTES = "deployment.environment=dev";
  try {
    const model = getModel("chat-default");
    assert.equal((model as any).maxRetries ?? 0, 0);
    await withStubbedFetch(async (seen) => {
      const r = await start(async () => {
        const out = await model.invoke("hello");
        return { messages: [new AIMessage(String(out.content))] };
      }, { team: "sales" });
      try {
        const res = await post(r.url, { prompt: "q" });
        assert.equal(res.json.output, "hi there", JSON.stringify(res.json));
        const call = seen.find((s) => s.url.startsWith("http://gateway.test/v1"))!;
        assert.ok(call.url.endsWith("/chat/completions"));
        assert.equal(call.headers.get("authorization"), "Bearer gw-key");
        const traceId = res.headers.get("x-ent-trace-id");
        assert.equal(call.headers.get("x-portkey-trace-id"), traceId);
        assert.match(call.headers.get("traceparent")!, new RegExp(`^00-${traceId}-[0-9a-f]{16}-0[01]$`));
        const meta = JSON.parse(call.headers.get("x-portkey-metadata")!);
        assert.equal(meta.agent, "test-agent");
        assert.equal(meta.agent_version, "1.2.3");
        assert.equal(meta.team, "sales");
        assert.equal(seen.filter((s) => s.url.includes("gateway.test")).length, 1);
      } finally {
        await r.close();
      }
    });
  } finally {
    delete process.env.ENT_MODEL_GATEWAY_URL;
    delete process.env.ENT_MODEL_GATEWAY_KEY;
    delete process.env.OTEL_RESOURCE_ATTRIBUTES;
  }
});

test("default gateway URL; no correlation headers outside a trace", async () => {
  assert.deepEqual(correlationHeaders(), {});
  await withStubbedFetch(async (seen) => {
    await getModel("chat-default").invoke("x");
    assert.ok(seen[0].url.startsWith("http://localhost:8788/v1/"));
    assert.equal(seen[0].headers.get("x-portkey-trace-id"), null);
  });
});

test("locked settings and empty names are rejected", () => {
  for (const key of ["baseURL", "apiKey", "maxRetries", "configuration", "fetch", "defaultHeaders"]) {
    assert.throws(() => getModel("m", { [key]: "x" } as any), ModelConfigError, key);
  }
  assert.throws(() => getModel(""), /must not be empty/);
  assert.ok(getModel("m", { temperature: 0, maxTokens: 10 }));
});

test("bind accepts registered tools on a real chat model", () => {
  const reg = new ToolRegistry();
  const t = reg.defineTool({ name: "t", description: "d", schema: z.object({ a: z.string() }), owner: "o", func: () => "" });
  assert.ok(reg.bind(getModel("chat-default"), [t]));
  void memoryProvider;
});
