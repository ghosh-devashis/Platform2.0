import type { Finding } from './types';

export interface DiagnosticData {
  /** 0-based range */
  startLine: number;
  startCharacter: number;
  endLine: number;
  endCharacter: number;
  message: string;
  severity: 'error' | 'warning';
  code: string;
  codeUrl?: string;
  source: string;
}

export const SOURCE = 'Enterprise Agent Toolkit';

/** Link target for a rule id: <base>#ent-001, where base is a URL or the local rules.md path. */
export function ruleDocUrl(rule: string, rulesDocUrl: string | undefined, platformDir: string | undefined): string | undefined {
  const anchor = '#' + rule.toLowerCase();
  if (rulesDocUrl && rulesDocUrl.trim()) return rulesDocUrl.trim().replace(/#.*$/, '') + anchor;
  if (platformDir) {
    const p = platformDir.replace(/\\/g, '/').replace(/\/+$/, '') + '/policy/docs/rules.md';
    return 'file:///' + p.replace(/^\/+/, '').replace(/ /g, '%20') + anchor;
  }
  return undefined;
}

/** Convert 1-based finding positions to 0-based VS Code positions. A missing or empty range becomes one character. */
export function toDiagnostic(f: Finding, rulesDocUrl?: string, platformDir?: string): DiagnosticData {
  const startLine = Math.max(0, f.line - 1);
  const startCharacter = Math.max(0, f.column - 1);
  let endLine = (f.end_line ?? f.line) - 1;
  let endCharacter = (f.end_column ?? f.column + 1) - 1;
  if (endLine < startLine || (endLine === startLine && endCharacter <= startCharacter)) {
    endLine = startLine;
    endCharacter = startCharacter + 1;
  }
  return {
    startLine,
    startCharacter,
    endLine,
    endCharacter,
    message: f.message,
    severity: f.severity === 'error' ? 'error' : 'warning',
    code: f.rule,
    codeUrl: ruleDocUrl(f.rule, rulesDocUrl, platformDir),
    source: SOURCE,
  };
}

/** Files the extension checks. */
export function isCheckable(fileName: string): boolean {
  const base = fileName.replace(/\\/g, '/').split('/').pop() ?? '';
  return base.endsWith('.py') || base === 'agent.yaml' || base === 'pyproject.toml';
}
