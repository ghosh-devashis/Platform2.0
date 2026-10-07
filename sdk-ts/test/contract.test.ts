/**
 * Cross-language contract test: the TypeScript and Python hello-agents must answer identically (status, body,
 * content-type, trace-id header presence) for the same requests. Skipped when `uv` is missing.
 */
import assert from "node:assert/strict";
import { spawn, spawnSync, type ChildProcess } from "node:child_process";
import { join, resolve } from "node:path";
import { after, before, test } from "node:test";

const SDK_TS = resolve(import.meta.dirname, "..", "..");
const REPO = resolve(SDK_TS, "..");
const TS_PORT = 18082;
const PY_PORT = 18081;
const SESSION = "x-amzn-bedrock-agentcore-runtime-session-id";

const uvAvailable = spawnSync("uv", ["--version"], { shell: process.platform === "win32" }).status === 0;
const children: ChildProcess[] = [];

function launch(cmd: string, args: string[], cwd: string, port: number): ChildProcess {
  const child = spawn(cmd, args, {
    cwd,
    env: { ...process.env, PORT: String(port), OTEL_SDK_DISABLED: "true", ENT_AUDIT_LOG_PATH: process.platform === "win32" ? "NUL" : "/dev/null" },
    stdio: "ignore",
    shell: process.platform === "win32" && cmd === "uv",
  });
  children.push(child);
  return child;
}

async function waitReady(port: number, timeoutMs: number): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      if ((await fetch(`http://127.0.0.1:${port}/ping`)).ok) return;
    } catch {
      /* not up yet */
    }
    await new Promise((r) => setTimeout(r, 500));
  }
  throw new Error(`agent on port ${port} did not become ready`);
}

before(async () => {
  if (!uvAvailable) return;
  launch(process.execPath, [join(SDK_TS, "dist", "examples", "hello-agent", "src", "index.js")], SDK_TS, TS_PORT);
  launch("uv", ["run", "hello-agent"], REPO, PY_PORT);
  await Promise.all([waitReady(TS_PORT, 30_000), waitReady(PY_PORT, 180_000)]);
});

after(() => {
  for (const c of children) {
    if (c.pid === undefined) continue;
    if (process.platform === "win32") spawnSync("taskkill", ["/pid", String(c.pid), "/T", "/F"]);
    else c.kill("SIGTERM");
  }
});

async function call(port: number, body: string, headers: Record<string, string> = {}) {
  const res = await fetch(`http://127.0.0.1:${port}/invocations`, {
    method: "POST",
    headers: { "content-type": "application/json", ...headers },
    body,
  });
  const text = await res.text();
  return {
    status: res.status,
    contentType: res.headers.get("content-type"),
    hasTraceId: /^[0-9a-f]{32}$/.test(res.headers.get("x-ent-trace-id") ?? ""),
    text,
  };
}

const cases: [string, string, Record<string, string>][] = [
  ["valid prompt", JSON.stringify({ prompt: "hello" }), {}],
  ["invalid JSON", "not json", {}],
  ["non-object body", JSON.stringify(["a", "list"]), {}],
  ["PII in the prompt", JSON.stringify({ prompt: "write to jane.doe@example.com or call 415-555-2671" }), {}],
  ["secret in the prompt", JSON.stringify({ prompt: "key sk-abcdefghijklmnopqrstuvwxyz" }), {}],
  ["session header echo", JSON.stringify({ prompt: "hi" }), { [SESSION]: "sess-42" }],
  ["unicode prompt", JSON.stringify({ prompt: "héllo wörld ✓" }), {}],
];

for (const [name, body, headers] of cases) {
  test(`contract parity: ${name}`, { skip: uvAvailable ? false : "uv is not installed" }, async () => {
    const [ts, py] = await Promise.all([call(TS_PORT, body, headers), call(PY_PORT, body, headers)]);
    assert.equal(ts.status, py.status, "status");
    assert.equal(ts.text, py.text, "body");
    assert.equal(ts.contentType, py.contentType, "content-type");
    assert.equal(ts.hasTraceId, py.hasTraceId, "x-ent-trace-id presence");
  });
}

test("contract parity: /ping body shape and unknown route", { skip: uvAvailable ? false : "uv is not installed" }, async () => {
  const get = async (port: number, path: string) => {
    const res = await fetch(`http://127.0.0.1:${port}${path}`);
    return { status: res.status, json: (await res.json()) as any };
  };
  const [ts, py] = await Promise.all([get(TS_PORT, "/ping"), get(PY_PORT, "/ping")]);
  assert.equal(ts.status, py.status);
  assert.deepEqual(Object.keys(ts.json), Object.keys(py.json));
  assert.equal(ts.json.status, py.json.status);
  const [t404, p404] = await Promise.all([get(TS_PORT, "/nope"), get(PY_PORT, "/nope")]);
  assert.equal(t404.status, p404.status);
  assert.deepEqual(t404.json, p404.json);
});
