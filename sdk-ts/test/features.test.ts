import assert from "node:assert/strict";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { AIMessage } from "@langchain/core/messages";
import { BaseChatModel } from "@langchain/core/language_models/chat_models";
import { END, MessagesAnnotation, START, StateGraph } from "@langchain/langgraph";
import { EnterpriseAgentApp, Guardrails } from "../src/index.js";
import * as manifest from "../src/manifest.js";
import * as context from "../src/context.js";
import { correlationHeaders, getModel } from "../src/models.js";
import { tokenBudget } from "../src/budget.js";
import { captureAudit, memoryProvider, post, echo, graphOf } from "./helpers.js";

// --- Manifest: token_budget, resources, memory (ported from test_features.py) ------------------------------------

const good = {
  name: "r-agent", team: "t", data_classification: "internal",
  token_budget: { hard: 5000 },
  resources: { buckets: ["agent-files"], tables: [{ name: "notes", partition_key: "id" }], memories: ["agent_memory"] },
  memory: { backend: "dynamodb", table: "agent-checkpoints" },
};

test("manifest: resources, memory and token budget parse into the manifest object", () => {
  const parsed = manifest.parse(good);
  assert.deepEqual(parsed.resources.buckets, ["agent-files"]);
  assert.equal(parsed.memory.backend, "dynamodb");
  assert.equal(parsed.memory.table, "agent-checkpoints");
  assert.deepEqual(parsed.tokenBudget, { hard: 5000 });
  const plain = manifest.parse({ name: "p-agent", team: "t", data_classification: "internal" });
  assert.deepEqual([plain.tokenBudget, plain.resources, plain.memory], [{}, {}, {}]);
});

test("manifest: validation messages for token_budget, resources and memory", () => {
  const cases: [Record<string, unknown>, string][] = [
    [{ token_budget: { soft: 10, hard: 5 } }, "'soft' must not exceed"],
    [{ token_budget: { hard: "lots" } }, "positive whole numbers"],
    [{ token_budget: { hard: 0 } }, "positive whole numbers"],
    [{ token_budget: { hard: 1.5 } }, "positive whole numbers"],
    [{ token_budget: { hard: true } }, "positive whole numbers"],
    [{ token_budget: {} }, "'token_budget'"],
    [{ token_budget: { medium: 3 } }, "must set 'soft' and/or 'hard'"],
    [{ resources: { buckets: ["Bad Bucket"] } }, "bucket name"],
    [{ resources: { tables: [{ name: "x" }] } }, "partition_key"],
    [{ resources: { tables: [{ name: "x", partition_key: "id", extra: 1 }] } }, "partition_key"],
    [{ resources: { memories: ["1bad"] } }, "memory name"],
    [{ resources: { queues: [] } }, "'resources' may only"],
    [{ memory: { backend: "redis" } }, "'memory' needs"],
    [{ memory: { backend: "memory", other: 1 } }, "'memory' needs"],
    [{ memory: { backend: "dynamodb", table: " " } }, "'memory.table' must be a table name."],
  ];
  for (const [change, expected] of cases) {
    assert.throws(() => manifest.parse({ ...good, ...change }), (e: any) => {
      assert.ok(e instanceof manifest.ManifestError);
      assert.ok(String(e.message).includes(expected), `${JSON.stringify(change)} -> ${e.message}`);
      return true;
    });
  }
  assert.deepEqual(manifest.validate({ ...good, token_budget: { soft: 5, hard: 5 }, memory: { backend: "memory" } }), []);
  assert.deepEqual(manifest.validate({ ...good, token_budget: { soft: 1 } }), []);
  assert.deepEqual(manifest.validate({ ...good, resources: { buckets: ["ab"] } }), [
    "'resources.buckets': 'ab' is not a valid bucket name (3-63 lowercase letters, digits, '.', '-').",
  ]);
  assert.deepEqual(manifest.validate({ ...good, resources: { queues: [], extra: 1 } }), [
    "'resources' may only contain buckets, memories, tables.",
  ]);
});

// --- Model headers ------------------------------------------------------------------------------------------------

const completion = {
  id: "c1", object: "chat.completion", created: 1, model: "m",
  choices: [{ index: 0, finish_reason: "stop", message: { role: "assistant", content: "ok" } }],
  usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 },
};

async function stubGateway<T>(fn: (seen: Headers[]) => Promise<T>): Promise<T> {
  const seen: Headers[] = [];
  const original = globalThis.fetch;
  globalThis.fetch = (async (input: any, init: any) => {
    const url = typeof input === "string" ? input : (input.url ?? String(input));
    if (url.startsWith("http://127.0.0.1")) return original(input, init);
    seen.push(new Headers(init?.headers ?? input.headers));
    return new Response(JSON.stringify(completion), { status: 200, headers: { "content-type": "application/json" } });
  }) as typeof fetch;
  try {
    return await fn(seen);
  } finally {
    globalThis.fetch = original;
  }
}

test("correlationHeaders: guardrail profile and data classification, even outside a trace", () => {
  assert.deepEqual(correlationHeaders(), {});
  const base = { agentName: "a", agentVersion: "1" };
  context.use({ ...base, guardrails: new Guardrails("strict"), dataClassification: "restricted" }, () => {
    assert.deepEqual(correlationHeaders(), { "x-ent-guardrail-profile": "strict", "x-ent-data-classification": "restricted" });
  });
  context.use({ ...base, guardrails: Guardrails.off({ waiver: "W-1" }), dataClassification: "internal" }, () => {
    assert.deepEqual(correlationHeaders(), { "x-ent-data-classification": "internal" });
  });
  context.use({ ...base }, () => assert.deepEqual(correlationHeaders(), {}));
});

test("model requests carry x-ent-guardrail-profile and x-ent-data-classification (app option and fromManifest)", async () => {
  const model = getModel("chat-default");
  await stubGateway(async (seen) => {
    const { provider } = memoryProvider();
    const app = new EnterpriseAgentApp(
      graphOf(async () => ({ messages: [new AIMessage(String((await model.invoke("x")).content))] })),
      { name: "h", team: "t", guardrails: new Guardrails("strict"), dataClassification: "confidential", tracerProvider: provider },
    );
    const server = await app.listen(0, "127.0.0.1");
    const url = `http://127.0.0.1:${(server.address() as any).port}`;
    try {
      assert.equal((await post(url, { prompt: "q" })).status, 200);
    } finally {
      await app.shutdown();
    }
    assert.equal(seen.length, 1);
    assert.equal(seen[0].get("x-ent-guardrail-profile"), "strict");
    assert.equal(seen[0].get("x-ent-data-classification"), "confidential");
    assert.ok(seen[0].get("x-portkey-trace-id"));

    // fromManifest takes the classification (and profile) from the manifest
    const file = join(mkdtempSync(join(tmpdir(), "ent-")), "agent.yaml");
    writeFileSync(file, "name: m-agent\nteam: t\ndata_classification: restricted\nguardrail_profile: strict\n");
    const app2 = EnterpriseAgentApp.fromManifest(
      graphOf(async () => ({ messages: [new AIMessage(String((await model.invoke("x")).content))] })),
      file, { tracerProvider: provider },
    );
    const server2 = await app2.listen(0, "127.0.0.1");
    try {
      await post(`http://127.0.0.1:${(server2.address() as any).port}`, { prompt: "q" });
    } finally {
      await app2.shutdown();
    }
    assert.equal(seen[1].get("x-ent-data-classification"), "restricted");
    assert.equal(seen[1].get("x-ent-guardrail-profile"), "strict");
  });
});

// --- Token budget (SDK-12) ----------------------------------------------------------------------------------------

class UsageModel extends BaseChatModel {
  constructor(private readonly tokensIn: number, private readonly tokensOut: number) {
    super({});
  }
  _llmType(): string {
    return "usage-fake";
  }
  async _generate(): Promise<any> {
    const message = new AIMessage({
      content: "hi",
      usage_metadata: { input_tokens: this.tokensIn, output_tokens: this.tokensOut, total_tokens: this.tokensIn + this.tokensOut },
    });
    return { generations: [{ text: "hi", message }] };
  }
}

function twoCallGraph(tokensIn = 8, tokensOut = 4) {
  const model = new UsageModel(tokensIn, tokensOut);
  const step = async (state: typeof MessagesAnnotation.State) => ({ messages: [await model.invoke(state.messages)] });
  return new StateGraph(MessagesAnnotation)
    .addNode("one", step).addNode("two", step)
    .addEdge(START, "one").addEdge("one", "two").addEdge("two", END)
    .compile();
}

async function serve(graph: any, options: Record<string, unknown>) {
  const { provider, exporter } = memoryProvider();
  const app = new EnterpriseAgentApp(graph, { name: "b", tracerProvider: provider, ...options });
  const server = await app.listen(0, "127.0.0.1");
  return { app, exporter, url: `http://127.0.0.1:${(server.address() as any).port}` };
}

test("hard budget stops the next model call: 429 BudgetExceeded and audit events", async () => {
  const cap = captureAudit();
  const { app, url, exporter } = await serve(twoCallGraph(), { tokenBudget: { soft: 5, hard: 10 } });
  try {
    const res = await post(url, { prompt: "hi" });
    assert.equal(res.status, 429);
    assert.equal(res.json.error.type, "BudgetExceeded");
    assert.ok(res.headers.get("x-ent-trace-id"));
    const kinds = cap.events.map((e) => e.event_type);
    assert.ok(kinds.includes("budget_soft_limit") && kinds.includes("budget_exceeded"), kinds.join());
    const soft = cap.events.find((e) => e.event_type === "budget_soft_limit")!;
    assert.deepEqual(soft.details, { tokens_used: 12, soft_limit: 5, hard_limit: 10 });
    assert.equal(soft.outcome, "warned");
    assert.equal(soft.agent, "b");
    const exceeded = cap.events.find((e) => e.event_type === "budget_exceeded")!;
    assert.deepEqual(exceeded.details, { tokens_used: 12, hard_limit: 10 });
    assert.equal(exceeded.outcome, "blocked");
    const root = exporter.getFinishedSpans().find((s) => s.name === "invoke_agent b")!;
    assert.equal(root.attributes["ent.outcome"], "budget_exceeded");
    assert.equal(root.attributes["gen_ai.usage.input_tokens"], 8);
    assert.equal(root.attributes["gen_ai.usage.output_tokens"], 4);
  } finally {
    await app.shutdown();
    cap.stop();
  }
});

test("soft budget warns once and lets the run finish", async () => {
  const cap = captureAudit();
  const { app, url } = await serve(twoCallGraph(3, 2), { tokenBudget: { soft: 4, hard: 1000 } });
  try {
    const res = await post(url, { prompt: "hi" });
    assert.equal(res.status, 200);
    const kinds = cap.events.map((e) => e.event_type);
    assert.equal(kinds.filter((k) => k === "budget_soft_limit").length, 1);
    assert.ok(!kinds.includes("budget_exceeded"));
  } finally {
    await app.shutdown();
    cap.stop();
  }
});

test("budget validation and manifest wiring; no budget means no tracking", async () => {
  assert.throws(() => tokenBudget({ soft: 10, hard: 5 }), /'soft' must not exceed 'hard'/);
  assert.throws(() => tokenBudget({ hard: 0 }), /'hard' must be positive/);
  const file = join(mkdtempSync(join(tmpdir(), "ent-")), "agent.yaml");
  writeFileSync(file, "name: b-agent\nteam: t\ndata_classification: internal\ntoken_budget: {soft: 100, hard: 200}\n");
  const { provider } = memoryProvider();
  const app = EnterpriseAgentApp.fromManifest(graphOf(echo), file, { tracerProvider: provider });
  assert.deepEqual(app.tokenBudget, { soft: 100, hard: 200 });
  const none = new EnterpriseAgentApp(graphOf(echo), { name: "n", tracerProvider: provider });
  assert.equal(none.tokenBudget, null);
});
