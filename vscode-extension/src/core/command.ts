export interface CommandSpec {
  command: string;
  args: string[];
}

/** Split a command line into words, honouring double and single quotes. */
export function splitCommandLine(text: string): string[] {
  const out: string[] = [];
  let cur = '';
  let quote: string | null = null;
  let has = false;
  for (const ch of text) {
    if (quote) {
      if (ch === quote) quote = null;
      else cur += ch;
    } else if (ch === '"' || ch === "'") {
      quote = ch;
      has = true;
    } else if (/\s/.test(ch)) {
      if (has || cur) out.push(cur);
      cur = '';
      has = false;
    } else {
      cur += ch;
      has = true;
    }
  }
  if (has || cur) out.push(cur);
  return out;
}

/**
 * Candidates to try, in order: the configured command, then `uv run ent-agent`.
 * `subArgs` (for example ["check", "x.py", "--json"]) is appended to each.
 */
export function resolveAgentCommand(setting: string | undefined, subArgs: string[]): CommandSpec[] {
  const words = splitCommandLine((setting ?? '').trim() || 'ent-agent');
  const primary: CommandSpec = { command: words[0], args: [...words.slice(1), ...subArgs] };
  const fallback: CommandSpec = { command: 'uv', args: ['run', 'ent-agent', ...subArgs] };
  return primary.command === 'uv' ? [primary] : [primary, fallback];
}

/** Resolve `name` on PATH (Windows PATHEXT aware). `exists` is injected for tests. */
export function findOnPath(
  name: string,
  env: { PATH?: string; Path?: string; PATHEXT?: string },
  exists: (p: string) => boolean,
  win: boolean = process.platform === 'win32',
): string | undefined {
  if (name.includes('/') || name.includes('\\')) return exists(name) ? name : undefined;
  const sep = win ? ';' : ':';
  const dirSep = win ? '\\' : '/';
  const dirs = (env.PATH ?? env.Path ?? '').split(sep).filter(Boolean);
  const exts = win ? ['', ...(env.PATHEXT ?? '.EXE;.CMD;.BAT').split(';').filter(Boolean)] : [''];
  for (const d of dirs) {
    for (const e of exts) {
      const p = d.replace(/[\\/]+$/, '') + dirSep + name + e;
      if (exists(p)) return p;
    }
  }
  return undefined;
}
