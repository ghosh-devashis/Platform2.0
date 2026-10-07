/**
 * Build metadata (SDK-14): which SDK and policy-bundle versions an agent image was built with.
 *
 * Written into the image at build time (`ent-agent-ts metadata write`) and read back at runtime or by CI.
 * Values come from build arguments exposed as environment variables: `ENT_POLICY_VERSION`, `ENT_GIT_SHA`,
 * `ENT_BUILD_TIME`. The same fields become trace resource attributes. JSON field names match the Python SDK.
 */

import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

export const SDK_PACKAGE = "@enterprise/agent-sdk";
export const DEFAULT_PATH = "/app/.ent/metadata.json";
export const TRACKED_PACKAGES = [
  "@langchain/langgraph",
  "@langchain/core",
  "@langchain/openai",
  "openai",
  "zod",
  "@opentelemetry/sdk-trace-base",
  "@aws-sdk/client-secrets-manager",
] as const;

/** Installed version of a package, found by walking up from its resolved entry point. */
export function packageVersion(name: string): string | null {
  try {
    const start = name === SDK_PACKAGE ? import.meta.url : import.meta.resolve(name);
    let dir = dirname(fileURLToPath(start));
    for (let i = 0; i < 12; i++) {
      const file = join(dir, "package.json");
      if (existsSync(file)) {
        const pkg = JSON.parse(readFileSync(file, "utf-8"));
        if (pkg.name === name) return pkg.version ?? null;
      }
      const parent = dirname(dir);
      if (parent === dir) break;
      dir = parent;
    }
  } catch {
    /* not installed */
  }
  return null;
}

export function sdkVersion(): string {
  return packageVersion(SDK_PACKAGE) ?? "unknown";
}

export interface BuildMetadata {
  sdk: { name: string; version: string };
  policy_bundle_version: string;
  git_sha: string;
  build_time: string;
  runtime: string;
  node: string;
  frameworks: Record<string, string | null>;
}

/** Metadata for the running environment. */
export function collect(): BuildMetadata {
  return {
    sdk: { name: SDK_PACKAGE, version: sdkVersion() },
    policy_bundle_version: process.env.ENT_POLICY_VERSION ?? "unknown",
    git_sha: process.env.ENT_GIT_SHA ?? "unknown",
    build_time: process.env.ENT_BUILD_TIME ?? "unknown",
    runtime: "node",
    node: process.versions.node,
    frameworks: Object.fromEntries(TRACKED_PACKAGES.map((p) => [p, packageVersion(p)])),
  };
}

/** Write the metadata file (used while building an image). */
export function write(path: string = DEFAULT_PATH): BuildMetadata {
  const data = collect();
  mkdirSync(dirname(path), { recursive: true });
  writeFileSync(path, JSON.stringify(data, null, 2), "utf-8");
  return data;
}

/** The baked-in metadata if present, otherwise the current environment's. */
export function read(path?: string): Record<string, any> {
  const target = path ?? process.env.ENT_METADATA_PATH ?? DEFAULT_PATH;
  if (existsSync(target)) return JSON.parse(readFileSync(target, "utf-8"));
  return collect() as unknown as Record<string, any>;
}

/** Trace resource attributes for the known fields. */
export function resourceAttributes(): Record<string, string> {
  const data = read();
  const attributes: Record<string, unknown> = {
    "ent.policy.version": data.policy_bundle_version,
    "ent.git.sha": data.git_sha,
    "ent.build.time": data.build_time,
  };
  const out: Record<string, string> = {};
  for (const [k, v] of Object.entries(attributes)) if (v && v !== "unknown") out[k] = String(v);
  return out;
}
