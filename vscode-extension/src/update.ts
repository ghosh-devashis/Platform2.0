import * as vscode from 'vscode';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import { cfg } from './config';
import { feedLocation, isHttpUrl, isUpdateAvailable, parseUpdateFeed, vsixLocation } from './core/feed';
import type { Telemetry } from './telemetry';

const DAY = 86_400_000;

async function readFeed(loc: string): Promise<string> {
  if (isHttpUrl(loc)) {
    const res = await fetch(loc, { signal: AbortSignal.timeout(10_000) });
    if (!res.ok) throw new Error(`feed returned HTTP ${res.status}`);
    return res.text();
  }
  return fs.readFileSync(loc, 'utf8');
}

async function fetchVsix(loc: string, name: string): Promise<string> {
  if (!isHttpUrl(loc)) return loc;
  const res = await fetch(loc, { signal: AbortSignal.timeout(120_000) });
  if (!res.ok) throw new Error(`download returned HTTP ${res.status}`);
  const dest = path.join(os.tmpdir(), path.basename(name));
  fs.writeFileSync(dest, Buffer.from(await res.arrayBuffer()));
  return dest;
}

/** Check the private feed; `manual` also reports "up to date" and errors. Automatic checks run at most once a day. */
export async function checkExtensionUpdate(ctx: vscode.ExtensionContext, telemetry: Telemetry, manual: boolean): Promise<void> {
  const setting = cfg().get<string>('updateFeedUrl', '').trim();
  if (!setting) {
    if (manual) void vscode.window.showInformationMessage('Set ent.updateFeedUrl to your extension feed (URL or folder).');
    return;
  }
  if (!manual && Date.now() - ctx.globalState.get<number>('ent.lastUpdateCheck', 0) < DAY) return;
  await ctx.globalState.update('ent.lastUpdateCheck', Date.now());
  try {
    const loc = feedLocation(setting);
    const feed = parseUpdateFeed(await readFeed(loc));
    const installed = String(ctx.extension.packageJSON.version);
    if (!isUpdateAvailable(installed, feed)) {
      if (manual) void vscode.window.showInformationMessage(`Enterprise Agent Toolkit ${installed} is up to date.`);
      return;
    }
    telemetry.record({ event: 'updateOffered' });
    const choice = await vscode.window.showInformationMessage(`Enterprise Agent Toolkit ${feed.latest} is available (installed ${installed}).`, 'Install', 'Later');
    if (choice !== 'Install') return;
    const file = await fetchVsix(vsixLocation(loc, feed.vsix), feed.vsix);
    await vscode.commands.executeCommand('workbench.extensions.installExtension', vscode.Uri.file(file));
    const r = await vscode.window.showInformationMessage(`Installed Enterprise Agent Toolkit ${feed.latest}. Reload to use it.`, 'Reload');
    if (r === 'Reload') await vscode.commands.executeCommand('workbench.action.reloadWindow');
  } catch (e) {
    if (manual) void vscode.window.showWarningMessage(`Extension update check failed: ${e instanceof Error ? e.message : String(e)}`);
  }
}
