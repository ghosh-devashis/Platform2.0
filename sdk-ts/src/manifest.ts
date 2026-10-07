/**
 * The agent manifest, `agent.yaml` (TPL-02): what an agent is, who owns it and what it may use.
 *
 * Keys (snake_case in the file, as in the Python SDK): name, team, data_classification, guardrail_profile,
 * description, models, tools, secrets. The same rules run in the IDE, pre-commit and CI: this module, the
 * Python SDK's `manifest.py`, `agent.schema.json` and `policy/opa/manifest.rego`. Keep them in step.
 */

import { readFileSync } from "node:fs";
import { parse as parseYaml } from "yaml";

export const NAME_PATTERN = /^[a-z][a-z0-9-]{1,47}$/;
export const CLASSIFICATIONS = ["public", "internal", "confidential", "restricted"] as const;
export const GUARDRAIL_PROFILES = ["standard", "strict"] as const;
const KEYS = new Set(["name", "team", "data_classification", "guardrail_profile", "description", "models", "tools", "secrets", "token_budget", "resources", "memory"]);
const LISTS = ["models", "tools", "secrets"] as const;
export const MEMORY_BACKENDS = ["memory", "dynamodb"] as const;
export const BUCKET_PATTERN = /^[a-z0-9][a-z0-9.-]{2,62}$/;
const MEMORY_NAME_PATTERN = /^[A-Za-z][A-Za-z0-9_]{0,47}$/;
export const RESOURCE_KEYS = ["buckets", "memories", "tables"];

/** The manifest is invalid. `problems` lists every issue found. */
export class ManifestError extends Error {
  readonly problems: string[];
  constructor(problems: string[]) {
    super("Invalid agent manifest:\n- " + problems.join("\n- "));
    this.name = "ManifestError";
    this.problems = [...problems];
  }
}

export interface AgentManifest {
  name: string;
  team: string;
  dataClassification: string;
  guardrailProfile: string;
  description: string;
  models: readonly string[];
  tools: readonly string[];
  secrets: readonly string[];
  /** {soft?, hard?} tokens per invocation. */
  tokenBudget: { soft?: number; hard?: number };
  /** {buckets?, tables?: [{name, partition_key, sort_key?}], memories?} the agent needs. */
  resources: { buckets?: string[]; tables?: { name: string; partition_key: string; sort_key?: string }[]; memories?: string[] };
  /** {backend: "memory"|"dynamodb", table?}. */
  memory: { backend?: string; table?: string };
  /** The AgentCore runtime name: letters, digits and underscores only (no hyphens). */
  readonly runtimeName: string;
}

const isMapping = (v: unknown): v is Record<string, unknown> => v !== null && typeof v === "object" && !Array.isArray(v);
const isText = (v: unknown): v is string => typeof v === "string";

/** All problems with a parsed manifest (empty list if valid). */
export function validate(data: unknown): string[] {
  if (!isMapping(data)) return ["The manifest must be a mapping of keys to values."];
  const problems: string[] = [];
  for (const unknown of Object.keys(data).filter((k) => !KEYS.has(k)).sort()) problems.push(`Unknown key '${unknown}'.`);
  const name = data.name;
  if (!isText(name) || !NAME_PATTERN.test(name)) {
    problems.push("'name' is required: 2-48 characters, lowercase letters, digits and hyphens, starting with a letter.");
  }
  if (!isText(data.team) || !data.team.trim()) problems.push("'team' is required (the owning team).");
  const classification = data.data_classification;
  if (!(CLASSIFICATIONS as readonly unknown[]).includes(classification)) {
    problems.push(`'data_classification' is required and must be one of ${CLASSIFICATIONS.join(", ")}.`);
  }
  const profile = data.guardrail_profile;
  if (profile !== undefined && profile !== null && !(GUARDRAIL_PROFILES as readonly unknown[]).includes(profile)) {
    problems.push(`'guardrail_profile' must be one of ${GUARDRAIL_PROFILES.join(", ")}.`);
  }
  if (classification === "restricted" && profile !== "strict") {
    problems.push("Agents handling 'restricted' data must set guardrail_profile: strict.");
  }
  for (const key of LISTS) {
    const value = key in data ? data[key] : [];
    if (!Array.isArray(value) || !value.every((v) => isText(v) && v.trim())) {
      problems.push(`'${key}' must be a list of non-empty strings.`);
    } else if (new Set(value).size !== value.length) {
      problems.push(`'${key}' contains duplicates.`);
    }
  }
  if ("description" in data && !isText(data.description)) problems.push("'description' must be text.");
  problems.push(...validateTokenBudget(data.token_budget), ...validateResources(data.resources), ...validateMemory(data.memory));
  return problems;
}

const isNone = (v: unknown): boolean => v === undefined || v === null;
const isPositiveInt = (v: unknown): boolean => typeof v === "number" && Number.isInteger(v) && v > 0;

function validateTokenBudget(value: unknown): string[] {
  if (isNone(value)) return [];
  if (!isMapping(value) || !Object.keys(value).length || Object.keys(value).some((k) => k !== "soft" && k !== "hard")) {
    return ["'token_budget' must set 'soft' and/or 'hard' (whole numbers of tokens)."];
  }
  if (!Object.values(value).every(isPositiveInt)) return ["'token_budget' limits must be positive whole numbers."];
  if ("soft" in value && "hard" in value && (value.soft as number) > (value.hard as number)) {
    return ["'token_budget': 'soft' must not exceed 'hard'."];
  }
  return [];
}

function validateResources(value: unknown): string[] {
  if (isNone(value)) return [];
  if (!isMapping(value) || Object.keys(value).some((k) => !RESOURCE_KEYS.includes(k))) {
    return [`'resources' may only contain ${RESOURCE_KEYS.join(", ")}.`];
  }
  const problems: string[] = [];
  const list = (key: string): unknown[] => (Array.isArray(value[key]) ? (value[key] as unknown[]) : []);
  for (const bucket of list("buckets")) {
    if (!isText(bucket) || !BUCKET_PATTERN.test(bucket)) {
      problems.push(`'resources.buckets': '${bucket}' is not a valid bucket name (3-63 lowercase letters, digits, '.', '-').`);
    }
  }
  for (const table of list("tables")) {
    const t = table as Record<string, unknown>;
    if (!(isMapping(table) && isText(t.name) && isText(t.partition_key) && Object.keys(t).every((k) => ["name", "partition_key", "sort_key"].includes(k)))) {
      problems.push("'resources.tables' entries need 'name' and 'partition_key' (optional 'sort_key').");
    }
  }
  for (const memory of list("memories")) {
    if (!isText(memory) || !MEMORY_NAME_PATTERN.test(memory)) {
      problems.push(`'resources.memories': '${memory}' is not a valid memory name (letters, digits, underscores).`);
    }
  }
  return problems;
}

function validateMemory(value: unknown): string[] {
  if (isNone(value)) return [];
  if (!isMapping(value) || Object.keys(value).some((k) => k !== "backend" && k !== "table") || !(MEMORY_BACKENDS as readonly unknown[]).includes(value.backend)) {
    return [`'memory' needs 'backend' (${MEMORY_BACKENDS.join(" or ")}) and optionally 'table'.`];
  }
  if ("table" in value && !(isText(value.table) && value.table.trim())) return ["'memory.table' must be a table name."];
  return [];
}

export function parse(data: unknown): AgentManifest {
  const problems = validate(data);
  if (problems.length) throw new ManifestError(problems);
  const d = data as Record<string, any>;
  const classification: string = d.data_classification;
  const name: string = d.name;
  return {
    name,
    team: (d.team as string).trim(),
    dataClassification: classification,
    guardrailProfile: d.guardrail_profile || (classification === "restricted" ? "strict" : "standard"),
    description: d.description ?? "",
    models: Object.freeze([...(d.models ?? [])]),
    tools: Object.freeze([...(d.tools ?? [])]),
    secrets: Object.freeze([...(d.secrets ?? [])]),
    tokenBudget: { ...(d.token_budget ?? {}) },
    resources: { ...(d.resources ?? {}) },
    memory: { ...(d.memory ?? {}) },
    get runtimeName() {
      return name.replaceAll("-", "_");
    },
  };
}

/** Read and validate a manifest file. */
export function load(path = "agent.yaml"): AgentManifest {
  let text: string;
  try {
    text = readFileSync(path, "utf-8");
  } catch (err) {
    if ((err as NodeJS.ErrnoException).code === "ENOENT") throw new ManifestError([`Manifest file '${path}' was not found.`]);
    throw err;
  }
  let data: unknown;
  try {
    data = parseYaml(text);
  } catch {
    throw new ManifestError([`Manifest file '${path}' is not valid YAML.`]);
  }
  return parse(data);
}
