import assert from "node:assert/strict";
import { test } from "node:test";
import { SecretNotFoundError, SecretsClient, SecretsError } from "../src/secrets.js";

function stub(handler: (command: any) => any) {
  const calls: any[] = [];
  return { calls, send: async (command: any) => (calls.push(command), handler(command)) };
}
const awsError = (name: string) => Object.assign(new Error("aws message with secret-ish text"), { name });

test("get returns a Secrets Manager value and caches it", async () => {
  const sm = stub(() => ({ SecretString: "s3cret" }));
  const c = new SecretsClient({ clients: { secretsmanager: sm } });
  assert.equal(await c.get("crm/api"), "s3cret");
  assert.equal(await c.get("crm/api"), "s3cret");
  assert.equal(sm.calls.length, 1);
  assert.equal(sm.calls[0].input.SecretId, "crm/api");
  assert.equal(await c.get("crm/api", { refresh: true }), "s3cret");
  assert.equal(sm.calls.length, 2);
});

test("cache expires after the TTL so rotated secrets are picked up", async () => {
  let v = "one";
  const sm = stub(() => ({ SecretString: v }));
  const c = new SecretsClient({ ttlSeconds: 0.05, clients: { secretsmanager: sm } });
  assert.equal(await c.get("x"), "one");
  v = "two";
  assert.equal(await c.get("x"), "one");
  await new Promise((r) => setTimeout(r, 80));
  assert.equal(await c.get("x"), "two");
  v = "three";
  c.clearCache();
  assert.equal(await c.get("x"), "three");
});

test("key reads a field of a JSON secret", async () => {
  const sm = stub(() => ({ SecretString: JSON.stringify({ api_key: "k", port: 5432, nested: { a: 1 } }) }));
  const c = new SecretsClient({ clients: { secretsmanager: sm } });
  assert.equal(await c.get("db", { key: "api_key" }), "k");
  assert.equal(await c.get("db", { key: "port" }), "5432");
  assert.equal(await c.get("db", { key: "nested" }), '{"a":1}');
  await assert.rejects(c.get("db", { key: "missing" }), /has no key 'missing'/);
  const plain = new SecretsClient({ clients: { secretsmanager: stub(() => ({ SecretString: "not json" })) } });
  await assert.rejects(plain.get("p", { key: "k" }), /is not JSON, so key 'k' can't be read/);
});

test("ssm: prefix uses Parameter Store with decryption", async () => {
  const ssm = stub(() => ({ Parameter: { Value: "postgres://x" } }));
  const c = new SecretsClient({ clients: { ssm } });
  assert.equal(await c.get("ssm:/crm/db-url"), "postgres://x");
  assert.deepEqual(ssm.calls[0].input, { Name: "/crm/db-url", WithDecryption: true });
});

test("fails closed: not found, other service errors, network errors, binary, empty name", async () => {
  const nf = new SecretsClient({ clients: { secretsmanager: stub(() => Promise.reject(awsError("ResourceNotFoundException"))) } });
  await assert.rejects(nf.get("gone"), (e: any) => e instanceof SecretNotFoundError && e.message === "Secret 'gone' was not found.");
  const pnf = new SecretsClient({ clients: { ssm: stub(() => Promise.reject(awsError("ParameterNotFound"))) } });
  await assert.rejects(pnf.get("ssm:/gone"), SecretNotFoundError);
  const denied = new SecretsClient({ clients: { secretsmanager: stub(() => Promise.reject(awsError("AccessDeniedException"))) } });
  await assert.rejects(denied.get("x"), (e: any) => {
    assert.ok(e instanceof SecretsError && !(e instanceof SecretNotFoundError));
    assert.equal(e.message, "Could not fetch secret 'x' (AccessDeniedException).");
    return true;
  });
  const net = new SecretsClient({ clients: { secretsmanager: stub(() => Promise.reject(new TypeError("fetch failed"))) } });
  await assert.rejects(net.get("x"), /Could not fetch secret 'x' \(TypeError\)\./);
  const bin = new SecretsClient({ clients: { secretsmanager: stub(() => ({ SecretBinary: new Uint8Array([0xff, 0xfe]) })) } });
  await assert.rejects(bin.get("x"), /has no text value/);
  const text = new SecretsClient({ clients: { secretsmanager: stub(() => ({ SecretBinary: new TextEncoder().encode("bin") })) } });
  assert.equal(await text.get("x"), "bin");
  await assert.rejects(text.get(""), /must not be empty/);
});

test("live Floci: secrets.get against the local stack (skipped when Floci is down)", async (t) => {
  let up = false;
  try {
    up = (await fetch("http://localhost:4566/_floci/health", { signal: AbortSignal.timeout(1500) })).ok;
  } catch {
    /* down */
  }
  if (!up) return t.skip("Floci is not running");
  const saved = { ...process.env };
  Object.assign(process.env, {
    AWS_ACCESS_KEY_ID: "test", AWS_SECRET_ACCESS_KEY: "test", AWS_DEFAULT_REGION: "us-east-1", AWS_ENDPOINT_URL: "http://localhost:4566",
  });
  try {
    const c = new SecretsClient();
    const greeting = await c.get("hello-agent/demo", { key: "greeting" });
    assert.ok(greeting.length > 0);
    await assert.rejects(c.get("does/not/exist-" + Date.now()), SecretNotFoundError);
  } finally {
    for (const k of Object.keys(process.env)) if (!(k in saved)) delete process.env[k];
    Object.assign(process.env, saved);
  }
});
