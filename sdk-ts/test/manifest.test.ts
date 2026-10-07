import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import * as manifest from "../src/manifest.js";
import * as metadata from "../src/metadata.js";
import * as audit from "../src/audit.js";
import { main } from "../src/cli.js";

const valid = { name: "crm-agent", team: "sales", data_classification: "confidential" };

test("a minimal manifest parses with defaults", () => {
  const m = manifest.parse(valid);
  assert.equal(m.guardrailProfile, "standard");
  assert.deepEqual([m.models, m.tools, m.secrets], [[], [], []]);
  assert.equal(m.runtimeName, "crm_agent");
  assert.equal(manifest.parse({ ...valid, data_classification: "restricted", guardrail_profile: "strict" }).guardrailProfile, "strict");
});

test("every rule reports its message", () => {
  const problems = (d: unknown) => manifest.validate(d);
  assert.deepEqual(problems([]), ["The manifest must be a mapping of keys to values."]);
  assert.deepEqual(problems({ ...valid, extra: 1, another: 2 }), ["Unknown key 'another'.", "Unknown key 'extra'."]);
  for (const bad of ["A", "1abc", "a", "has_underscore", "x".repeat(49), 5]) {
    assert.deepEqual(problems({ ...valid, name: bad }), [
      "'name' is required: 2-48 characters, lowercase letters, digits and hyphens, starting with a letter.",
    ]);
  }
  assert.deepEqual(problems({ ...valid, team: "  " }), ["'team' is required (the owning team)."]);
  assert.deepEqual(problems({ ...valid, data_classification: "secret" }), [
    "'data_classification' is required and must be one of public, internal, confidential, restricted.",
  ]);
  assert.deepEqual(problems({ ...valid, guardrail_profile: "loose" }), ["'guardrail_profile' must be one of standard, strict."]);
  assert.deepEqual(problems({ ...valid, data_classification: "restricted" }), [
    "Agents handling 'restricted' data must set guardrail_profile: strict.",
  ]);
  assert.deepEqual(problems({ ...valid, models: "chat" }), ["'models' must be a list of non-empty strings."]);
  assert.deepEqual(problems({ ...valid, tools: ["a", " "] }), ["'tools' must be a list of non-empty strings."]);
  assert.deepEqual(problems({ ...valid, secrets: ["a", "a"] }), ["'secrets' contains duplicates."]);
  assert.deepEqual(problems({ ...valid, description: 5 }), ["'description' must be text."]);
  assert.throws(() => manifest.parse({}), (e: any) => e instanceof manifest.ManifestError && e.problems.length === 3);
});

test("load: missing file, invalid YAML, valid file", () => {
  const dir = mkdtempSync(join(tmpdir(), "ent-"));
  assert.throws(() => manifest.load(join(dir, "nope.yaml")), /was not found/);
  const bad = join(dir, "bad.yaml");
  writeFileSync(bad, "name: [unclosed\n  - : :");
  assert.throws(() => manifest.load(bad), /is not valid YAML/);
  const empty = join(dir, "empty.yaml");
  writeFileSync(empty, "");
  assert.throws(() => manifest.load(empty), /must be a mapping/);
  const ok = join(dir, "agent.yaml");
  writeFileSync(ok, "name: crm-agent\nteam: sales\ndata_classification: internal\nmodels: [chat-default]\ndescription: hi\n");
  assert.deepEqual(manifest.load(ok).models, ["chat-default"]);
});

test("loads the repository's example manifest", () => {
  const m = manifest.load(join(import.meta.dirname, "..", "..", "..", "examples", "hello-agent", "agent.yaml"));
  assert.ok(m.name);
});

test("cli: manifest validate exit codes", () => {
  const dir = mkdtempSync(join(tmpdir(), "ent-"));
  const ok = join(dir, "agent.yaml");
  writeFileSync(ok, "name: crm-agent\nteam: sales\ndata_classification: internal\n");
  const log = console.log;
  const err = console.error;
  console.log = console.error = () => {};
  try {
    assert.equal(main(["manifest", "validate", ok]), 0);
    assert.equal(main(["manifest", "validate", join(dir, "missing.yaml")]), 1);
    assert.equal(main(["bogus"]), 2);
  } finally {
    console.log = log;
    console.error = err;
  }
});

test("metadata: collect, write/read round trip, resource attributes", () => {
  const dir = mkdtempSync(join(tmpdir(), "ent-"));
  process.env.ENT_POLICY_VERSION = "p1";
  process.env.ENT_GIT_SHA = "abc123";
  try {
    const data = metadata.collect();
    assert.equal(data.sdk.name, "@enterprise/agent-sdk");
    assert.equal(data.sdk.version, "0.1.0");
    assert.equal(data.policy_bundle_version, "p1");
    assert.ok(data.frameworks["@langchain/langgraph"]);
    const file = join(dir, "sub", "metadata.json");
    metadata.write(file);
    assert.equal(JSON.parse(readFileSync(file, "utf-8")).git_sha, "abc123");
    assert.equal(metadata.read(file).git_sha, "abc123");
    assert.deepEqual(metadata.resourceAttributes(), { "ent.policy.version": "p1", "ent.git.sha": "abc123" });
  } finally {
    delete process.env.ENT_POLICY_VERSION;
    delete process.env.ENT_GIT_SHA;
  }
});

test("audit: events go to ENT_AUDIT_LOG_PATH as separate JSON lines; failing sinks are skipped", () => {
  const dir = mkdtempSync(join(tmpdir(), "ent-"));
  const file = join(dir, "audit.log");
  audit.reset();
  audit.configure(file);
  const seen: unknown[] = [];
  audit.addSink(() => {
    throw new Error("sink down");
  });
  audit.addSink((e) => seen.push(e));
  try {
    const e = audit.emit("tool_call", { outcome: "success", tool: "t", agent: "a1" });
    assert.equal(e.agent, "a1");
    const line = JSON.parse(readFileSync(file, "utf-8").trim());
    assert.equal(line.stream, "audit");
    assert.deepEqual(line.details, { tool: "t", agent: "a1" });
    assert.equal(seen.length, 1);
  } finally {
    audit.reset();
  }
});
