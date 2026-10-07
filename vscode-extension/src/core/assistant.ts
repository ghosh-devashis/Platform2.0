import { resolveAgentCommand, type CommandSpec } from './command';

export const AGENT_NAME_PATTERN = /^[a-z][a-z0-9-]{1,47}$/;
export const CLASSIFICATIONS = ['public', 'internal', 'confidential', 'restricted'] as const;

export function validateAgentName(name: string): string | undefined {
  return AGENT_NAME_PATTERN.test(name) ? undefined : 'Use 2-48 characters: lowercase letters, digits and hyphens, starting with a letter.';
}

export function validateTeam(team: string): string | undefined {
  return /^[A-Za-z0-9][A-Za-z0-9 _.-]{0,63}$/.test(team.trim()) ? undefined : 'Enter the owning team (letters, digits, - _ . and spaces).';
}

/** Arguments for `ent-agent new`. */
export function newAgentArgs(name: string, team: string, classification: string, parentDir: string): string[] {
  return ['new', name, '--team', team.trim(), '--classification', classification, '--dir', parentDir];
}

/** Read top-level `name:` and `team:` from agent.yaml without a YAML parser. */
export function parseManifestIdentity(yaml: string | undefined): { name?: string; team?: string } {
  if (!yaml) return {};
  const get = (key: string) => {
    const m = new RegExp(`^${key}:\\s*["']?([^"'#\\r\\n]+?)["']?\\s*(?:#.*)?$`, 'm').exec(yaml);
    return m ? m[1].trim() : undefined;
  };
  return { name: get('name'), team: get('team') };
}

export function fillStandards(template: string, identity: { name?: string; team?: string }): string {
  return template.replace(/__NAME__/g, identity.name ?? 'this agent').replace(/__TEAM__/g, identity.team ?? 'your-team');
}

/** Files that carry the standards for each assistant, relative to the workspace root. */
export const INSTRUCTION_FILES = ['CLAUDE.md', 'AGENTS.md', '.github/copilot-instructions.md'] as const;

interface McpJson {
  servers?: Record<string, { type?: string; command?: string; args?: string[] }>;
  [k: string]: unknown;
}

/** Merge an `ent-standards` stdio server into an existing .vscode/mcp.json text (comments are not supported in the input). */
export function buildMcpJson(existing: string | undefined, spec: CommandSpec): string {
  let cur: McpJson = {};
  if (existing && existing.trim()) {
    try {
      cur = JSON.parse(existing) as McpJson;
    } catch {
      throw new Error('.vscode/mcp.json is not plain JSON (comments?). Add the "ent-standards" server by hand.');
    }
  }
  cur.servers = { ...(cur.servers ?? {}), 'ent-standards': { type: 'stdio', command: spec.command, args: spec.args } };
  return JSON.stringify(cur, null, 2) + '\n';
}

/** Command that starts the MCP standards server. Prefers the configured command if it exists, else `uv run`. */
export function mcpCommand(setting: string | undefined, commandExists: (cmd: string) => boolean): CommandSpec {
  const candidates = resolveAgentCommand(setting, ['mcp']);
  return candidates.find((c) => commandExists(c.command)) ?? candidates[candidates.length - 1];
}

/** Platform checkout: the setting if set, else the first folder that contains harness/dev.ps1. */
export function findPlatformDir(setting: string | undefined, folders: string[], exists: (p: string) => boolean): string | undefined {
  if (setting && setting.trim()) return setting.trim();
  return folders.find((f) => {
    const s = f.includes('\\') ? '\\' : '/';
    return exists(f.replace(/[\\/]+$/, '') + s + 'harness' + s + 'dev.ps1');
  });
}
