import * as vscode from 'vscode';
import * as path from 'node:path';
import { cfg, platformDir, runtimeConfig } from './config';
import { buildServices, probe, summarize, type ProbeResult } from './core/health';
import type { Telemetry } from './telemetry';

export class RuntimeControls implements vscode.Disposable {
  private readonly item = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 50);
  private timer: ReturnType<typeof setInterval> | undefined;
  private polling = false;
  private last: ProbeResult[] = [];

  constructor(private readonly telemetry: Telemetry) {
    this.item.command = 'ent.showHealth';
    this.item.name = 'Enterprise Agent local stack';
    this.refreshEnabled();
  }

  /** Start or stop polling according to the setting. */
  refreshEnabled(): void {
    const enabled = cfg().get<boolean>('runtime.statusBar.enabled', true);
    if (enabled && !this.timer) {
      this.item.show();
      void this.poll();
      this.timer = setInterval(() => void this.poll(), 10_000);
    } else if (!enabled && this.timer) {
      clearInterval(this.timer);
      this.timer = undefined;
      this.item.hide();
    }
  }

  async poll(): Promise<ProbeResult[]> {
    if (this.polling) return this.last;
    this.polling = true;
    try {
      this.last = await Promise.all(buildServices(runtimeConfig()).map((s) => probe(s, 2000)));
      const s = summarize(this.last);
      this.item.text = s.text;
      this.item.tooltip = s.tooltip + '\n\nClick for details';
      this.item.backgroundColor = s.level === 'error' ? new vscode.ThemeColor('statusBarItem.errorBackground') : s.level === 'warn' ? new vscode.ThemeColor('statusBarItem.warningBackground') : undefined;
    } finally {
      this.polling = false;
    }
    return this.last;
  }

  private devScript(): string | undefined {
    const dir = platformDir();
    if (!dir) {
      void vscode.window.showWarningMessage('Platform folder not found. Set ent.platformDir to your Platform2.0 checkout.');
      return undefined;
    }
    return path.join(dir, 'harness', 'dev.ps1');
  }

  private runDev(args: string): void {
    const script = this.devScript();
    if (!script) return;
    const term = vscode.window.terminals.find((t) => t.name === 'Enterprise Agent Stack') ?? vscode.window.createTerminal({ name: 'Enterprise Agent Stack', cwd: path.dirname(path.dirname(script)) });
    term.show();
    const ps = process.platform === 'win32' ? 'powershell -NoProfile -File' : 'pwsh -NoProfile -File';
    term.sendText(`${ps} "${script}" ${args}`);
    setTimeout(() => void this.poll(), 15_000);
  }

  start(): void {
    this.telemetry.record({ event: 'startStack' });
    this.runDev(`up -Mode ${cfg().get<string>('runtime.mode', 'replay')}`);
  }

  stop(): void {
    this.telemetry.record({ event: 'stopStack' });
    this.runDev('down');
  }

  openFlociUi(): void {
    void vscode.env.openExternal(vscode.Uri.parse(cfg().get<string>('runtime.flociUiUrl', 'http://localhost:4500')));
  }

  openJaeger(): void {
    void vscode.env.openExternal(vscode.Uri.parse(cfg().get<string>('runtime.jaegerUrl', 'http://localhost:16686')));
  }

  async showHealth(): Promise<void> {
    const results = await this.poll();
    await vscode.window.showQuickPick(
      results.map((r) => ({ label: `${r.ok ? '$(check)' : '$(error)'} ${r.label}`, description: r.detail, detail: `${r.url} (${r.ms} ms)` })),
      { title: 'Local stack health', placeHolder: 'Use the Enterprise Agent commands to start or stop the stack' },
    );
  }

  dispose(): void {
    if (this.timer) clearInterval(this.timer);
    this.item.dispose();
  }
}
