export interface ToolLens {
  /** 0-based line of the decorator */
  line: number;
  owner?: string;
  classification?: string;
  sideEffects: boolean;
  title: string;
}

function strArg(args: string, name: string): string | undefined {
  const m = new RegExp(`\\b${name}\\s*=\\s*(?:"([^"]*)"|'([^']*)')`).exec(args);
  return m ? (m[1] ?? m[2]) : undefined;
}

/** Find every `@tools.tool(...)` decorator (possibly multi-line) and read owner / classification / side_effects. */
export function parseToolLenses(text: string): ToolLens[] {
  const lines = text.split(/\r?\n/);
  const out: ToolLens[] = [];
  for (let i = 0; i < lines.length; i++) {
    const m = /^\s*@tools\.tool\b\s*(\()?/.exec(lines[i]);
    if (!m) continue;
    let args = '';
    if (m[1]) {
      let depth = 0;
      let started = false;
      let quote: string | null = null;
      scan: for (let j = i; j < lines.length && j < i + 30; j++) {
        const l = j === i ? lines[j].slice(m[0].length - 1) : lines[j];
        for (const ch of l) {
          if (quote) {
            if (ch === quote) quote = null;
            args += ch;
            continue;
          }
          if (ch === '"' || ch === "'") quote = ch;
          if (ch === '(') {
            depth++;
            started = true;
            if (depth === 1) continue;
          } else if (ch === ')') {
            depth--;
            if (started && depth === 0) break scan;
          }
          args += ch;
        }
        args += '\n';
      }
    }
    const owner = strArg(args, 'owner');
    const classification = strArg(args, 'data_classification');
    const sideEffects = /\bside_effects\s*=\s*True\b/.test(args);
    const title = [
      `owner: ${owner ?? 'not set'}`,
      `classification: ${classification ?? 'not set'}`,
      `side effects: ${sideEffects ? 'yes' : 'no'}`,
    ].join('  |  ');
    out.push({ line: i, owner, classification, sideEffects, title });
  }
  return out;
}
