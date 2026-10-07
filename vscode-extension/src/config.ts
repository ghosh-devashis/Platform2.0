import * as vscode from 'vscode';
import * as fs from 'node:fs';
import { findPlatformDir } from './core/assistant';
import type { RuntimeConfig } from './core/health';

export const cfg = () => vscode.workspace.getConfiguration('ent');

export const agentCommand = (): string => cfg().get<string>('agentCommand', 'ent-agent');

export function platformDir(): string | undefined {
  const folders = (vscode.workspace.workspaceFolders ?? []).map((f) => f.uri.fsPath);
  return findPlatformDir(cfg().get<string>('platformDir', ''), folders, (p) => fs.existsSync(p));
}

export function runtimeConfig(): RuntimeConfig {
  const c = cfg();
  return {
    flociUrl: c.get('runtime.flociUrl', 'http://localhost:4566'),
    jaegerUrl: c.get('runtime.jaegerUrl', 'http://localhost:16686'),
    modelRouterUrl: c.get('runtime.modelRouterUrl', 'http://localhost:8788'),
    invokeRouterUrl: c.get('runtime.invokeRouterUrl', 'http://localhost:4567'),
    agentUrls: c.get<string[]>('runtime.agentUrls', ['http://localhost:8080', 'http://localhost:8081']),
  };
}

/** Working directory for CLI calls: the workspace folder that contains the file, else the first folder. */
export function cwdFor(uri?: vscode.Uri): string | undefined {
  const f = (uri && vscode.workspace.getWorkspaceFolder(uri)) ?? vscode.workspace.workspaceFolders?.[0];
  return f?.uri.fsPath;
}

export function readText(path: string): string | undefined {
  try {
    return fs.readFileSync(path, 'utf8');
  } catch {
    return undefined;
  }
}
