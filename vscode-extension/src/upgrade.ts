import * as vscode from 'vscode';
import * as fs from 'node:fs';
import * as path from 'node:path';
import { cfg, platformDir, readText } from './config';
import { assessUpgrade, detectSdkVersion, parseVersionsFile, type VersionsInfo } from './core/semver';
import type { Telemetry } from './telemetry';

function latestInfo(): VersionsInfo | undefined {
  const dir = platformDir();
  const fromFile = dir ? parseVersionsFile(readText(path.join(dir, 'policy', 'data', 'versions.json'))) : undefined;
  const override = cfg().get<string>('latestSdkVersion', '').trim();
  if (override) return { latest: override, supported: fromFile?.supported ?? [] };
  return fromFile;
}

export async function checkUpgrade(ctx: vscode.ExtensionContext, telemetry: Telemetry, manual: boolean): Promise<void> {
  const folder = vscode.workspace.workspaceFolders?.[0];
  if (!folder) return;
  const root = folder.uri.fsPath;
  const current = detectSdkVersion(readText(path.join(root, 'uv.lock')), readText(path.join(root, 'pyproject.toml')));
  if (!current) {
    if (manual) void vscode.window.showInformationMessage('This workspace does not use ent-agent-sdk.');
    return;
  }
  const info = latestInfo();
  if (!info) {
    if (manual) void vscode.window.showInformationMessage('No version information found. Set ent.latestSdkVersion or ent.platformDir.');
    return;
  }
  const a = assessUpgrade(current.version, info, new Date());
  if (a.level === 'ok') {
    if (manual) void vscode.window.showInformationMessage(a.message);
    return;
  }
  const key = `ent.upgrade.dismissed.${current.version}->${info.latest}`;
  if (!manual && ctx.workspaceState.get<boolean>(key)) return;
  telemetry.record({ event: 'upgradePrompt' });
  const show = a.level === 'unsupported' ? vscode.window.showErrorMessage : vscode.window.showWarningMessage;
  const choice = await show(a.message, 'Upgrade', 'Changelog', 'Not now');
  if (choice === 'Upgrade') {
    telemetry.record({ event: 'upgradeAccepted' });
    const win = process.platform === 'win32';
    const term = vscode.window.createTerminal({ name: 'ent-agent-sdk upgrade', cwd: root, shellPath: win ? 'powershell.exe' : undefined });
    term.show();
    term.sendText(win ? 'uv lock --upgrade-package ent-agent-sdk; if ($?) { uv sync }' : 'uv lock --upgrade-package ent-agent-sdk && uv sync');
  } else if (choice === 'Changelog') {
    const dir = platformDir();
    const file = dir ? path.join(dir, 'CHANGELOG.md') : undefined;
    if (file && fs.existsSync(file)) await vscode.commands.executeCommand('markdown.showPreview', vscode.Uri.file(file));
    else void vscode.window.showInformationMessage('CHANGELOG.md was not found in the platform folder (ent.platformDir).');
  } else if (choice === 'Not now') {
    await ctx.workspaceState.update(key, true);
  }
}
