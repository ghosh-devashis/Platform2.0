import { spawn } from 'node:child_process';
import { resolveAgentCommand } from './command';
import type { Finding } from './types';

export interface RunResult {
  stdout: string;
  stderr: string;
  code: number | null;
}

export interface RunOptions {
  cwd?: string;
  timeoutMs?: number;
  signal?: AbortSignal;
}

export class SpawnError extends Error {
  constructor(message: string, readonly code?: string) {
    super(message);
  }
}

/** Run one process; rejects with SpawnError (ENOENT, TIMEOUT, ABORT). */
export function runProcess(command: string, args: string[], opts: RunOptions = {}): Promise<RunResult> {
  return new Promise((resolve, reject) => {
    // .cmd/.bat shims on Windows need a shell; quote everything ourselves in that case.
    const useShell = process.platform === 'win32' && /\.(cmd|bat)$/i.test(command);
    const q = (s: string) => `"${s.replace(/"/g, '\\"')}"`;
    const child = spawn(useShell ? q(command) : command, useShell ? args.map(q) : args, {
      cwd: opts.cwd,
      shell: useShell,
      windowsHide: true,
      env: { ...process.env, VIRTUAL_ENV: '' },
    });
    let stdout = '';
    let stderr = '';
    let done = false;
    const finish = (fn: () => void) => {
      if (done) return;
      done = true;
      clearTimeout(timer);
      opts.signal?.removeEventListener('abort', onAbort);
      fn();
    };
    const onAbort = () => {
      child.kill();
      finish(() => reject(new SpawnError('aborted', 'ABORT')));
    };
    const timer = setTimeout(() => {
      child.kill();
      finish(() => reject(new SpawnError(`timed out after ${opts.timeoutMs ?? 30000}ms`, 'TIMEOUT')));
    }, opts.timeoutMs ?? 30000);
    if (opts.signal?.aborted) {
      onAbort();
      return;
    }
    opts.signal?.addEventListener('abort', onAbort);
    child.stdout.on('data', (d) => (stdout += d));
    child.stderr.on('data', (d) => (stderr += d));
    child.on('error', (e: NodeJS.ErrnoException) => finish(() => reject(new SpawnError(e.message, e.code))));
    child.on('close', (code) => finish(() => resolve({ stdout, stderr, code })));
  });
}

function isFinding(x: unknown): x is Finding {
  const f = x as Finding;
  return !!f && typeof f.rule === 'string' && typeof f.line === 'number' && typeof f.column === 'number' && typeof f.message === 'string';
}

/** Parse the JSON array printed by `ent-agent check --json`. */
export function parseFindings(stdout: string): Finding[] {
  const start = stdout.indexOf('[');
  const end = stdout.lastIndexOf(']');
  if (start < 0 || end < start) throw new Error('no JSON array in ent-agent output');
  const data: unknown = JSON.parse(stdout.slice(start, end + 1));
  if (!Array.isArray(data)) throw new Error('ent-agent output is not an array');
  return data.filter(isFinding);
}

/** Run `ent-agent <args>`: the configured command first, then `uv run ent-agent` if the first cannot be spawned. */
export async function runCli(setting: string | undefined, args: string[], opts: RunOptions = {}): Promise<RunResult> {
  let lastErr: unknown;
  for (const c of resolveAgentCommand(setting, args)) {
    try {
      return await runProcess(c.command, c.args, opts);
    } catch (e) {
      lastErr = e;
      if (!(e instanceof SpawnError) || e.code !== 'ENOENT') throw e;
    }
  }
  throw lastErr;
}

/**
 * Run `ent-agent check <paths> --json`. Exit code 1 (errors found) is normal; output that cannot be
 * parsed is reported as an error that includes stderr.
 */
export async function runCheck(setting: string | undefined, paths: string[], opts: RunOptions = {}): Promise<Finding[]> {
  const r = await runCli(setting, ['check', ...paths, '--json'], opts);
  try {
    return parseFindings(r.stdout);
  } catch (e) {
    throw new Error(`ent-agent check failed (exit ${r.code}): ${(r.stderr || String(e)).trim().slice(0, 500)}`);
  }
}
