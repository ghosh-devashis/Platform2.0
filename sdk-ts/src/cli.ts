#!/usr/bin/env node
/** Tiny CLI: `ent-agent-ts manifest validate <file>` and `ent-agent-ts metadata [write [path]|read]`. */

import * as manifest from "./manifest.js";
import * as metadata from "./metadata.js";

export function main(argv: string[]): number {
  const [cmd, sub, arg] = argv;
  if (cmd === "manifest" && sub === "validate") {
    try {
      const m = manifest.load(arg ?? "agent.yaml");
      console.log(`OK: ${m.name} (${m.team}, ${m.dataClassification}, guardrails: ${m.guardrailProfile})`);
      return 0;
    } catch (err) {
      if (err instanceof manifest.ManifestError) {
        console.error(err.message);
        return 1;
      }
      throw err;
    }
  }
  if (cmd === "metadata") {
    const data = sub === "write" ? metadata.write(arg ?? metadata.DEFAULT_PATH) : metadata.read();
    console.log(JSON.stringify(data, null, 2));
    return 0;
  }
  console.error("Usage: ent-agent-ts manifest validate [file] | metadata [write [path] | read]");
  return 2;
}

if (process.argv[1] && import.meta.url.endsWith(process.argv[1].replaceAll("\\", "/").split("/").pop()!)) {
  process.exitCode = main(process.argv.slice(2));
}
