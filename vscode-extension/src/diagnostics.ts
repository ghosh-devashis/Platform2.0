import * as vscode from 'vscode';
import * as path from 'node:path';
import { agentCommand, cfg, cwdFor, platformDir } from './config';
import { isCheckable, toDiagnostic } from './core/diagnostics';
import { runCheck } from './core/runner';
import { KeyedScheduler } from './core/scheduler';
import type { Finding } from './core/types';
import type { Telemetry } from './telemetry';

export class DiagnosticsController implements vscode.Disposable {
  readonly collection = vscode.languages.createDiagnosticCollection('enterprise-agent');
  private readonly findings = new Map<string, Finding[]>();
  private readonly scheduler: KeyedScheduler;
  private readonly output = vscode.window.createOutputChannel('Enterprise Agent Toolkit');
  private warnedFailure = false;

  constructor(private readonly telemetry: Telemetry) {
    this.scheduler = new KeyedScheduler(400, (key, signal) => this.checkUri(vscode.Uri.parse(key), signal));
  }

  findingsFor(uri: vscode.Uri): Finding[] {
    return this.findings.get(uri.toString()) ?? [];
  }

  /** Called on open and save. */
  schedule(doc: vscode.TextDocument): void {
    if (doc.uri.scheme !== 'file' || !isCheckable(doc.fileName) || !cfg().get<boolean>('diagnostics.enabled', true)) return;
    this.scheduler.request(doc.uri.toString());
  }

  async checkNow(uri: vscode.Uri): Promise<void> {
    await this.checkUri(uri, new AbortController().signal);
  }

  private apply(uri: vscode.Uri, list: Finding[]): void {
    const rulesDoc = cfg().get<string>('rulesDocUrl', '');
    const pdir = platformDir();
    this.findings.set(uri.toString(), list);
    this.collection.set(
      uri,
      list.map((f) => {
        const d = toDiagnostic(f, rulesDoc, pdir);
        const diag = new vscode.Diagnostic(
          new vscode.Range(d.startLine, d.startCharacter, d.endLine, d.endCharacter),
          d.message,
          d.severity === 'error' ? vscode.DiagnosticSeverity.Error : vscode.DiagnosticSeverity.Warning,
        );
        diag.source = d.source;
        diag.code = d.codeUrl ? { value: d.code, target: vscode.Uri.parse(d.codeUrl) } : d.code;
        return diag;
      }),
    );
  }

  private fail(e: unknown): void {
    const msg = e instanceof Error ? e.message : String(e);
    this.output.appendLine(msg);
    if (!this.warnedFailure) {
      this.warnedFailure = true;
      void vscode.window.showWarningMessage('Enterprise Agent checks could not run. See the "Enterprise Agent Toolkit" output, or set ent.agentCommand.', 'Show output').then((c) => c && this.output.show());
    }
  }

  private async checkUri(uri: vscode.Uri, signal: AbortSignal): Promise<void> {
    try {
      const list = await runCheck(agentCommand(), [uri.fsPath], {
        cwd: cwdFor(uri) ?? path.dirname(uri.fsPath),
        timeoutMs: cfg().get<number>('diagnostics.timeoutMs', 30000),
        signal,
      });
      this.apply(uri, list);
      this.telemetry.record({ event: 'diagnostics', ruleIds: list.map((f) => f.rule), count: list.length });
    } catch (e) {
      if ((e as { code?: string }).code !== 'ABORT') this.fail(e);
    }
  }

  async checkWorkspace(): Promise<void> {
    const folders = vscode.workspace.workspaceFolders ?? [];
    if (!folders.length) {
      void vscode.window.showInformationMessage('Open a folder first.');
      return;
    }
    await vscode.window.withProgress({ location: vscode.ProgressLocation.Notification, title: 'Enterprise Agent: checking workspace' }, async () => {
      this.collection.clear();
      this.findings.clear();
      let total = 0;
      const rules: string[] = [];
      for (const folder of folders) {
        try {
          const list = await runCheck(agentCommand(), [folder.uri.fsPath], { cwd: folder.uri.fsPath, timeoutMs: 120000 });
          const byFile = new Map<string, Finding[]>();
          for (const f of list) {
            const abs = path.resolve(folder.uri.fsPath, f.path);
            byFile.set(abs, [...(byFile.get(abs) ?? []), f]);
          }
          for (const [file, fl] of byFile) this.apply(vscode.Uri.file(file), fl);
          total += list.length;
          rules.push(...list.map((f) => f.rule));
        } catch (e) {
          this.fail(e);
        }
      }
      this.telemetry.record({ event: 'checkWorkspace', ruleIds: rules, count: total });
      void vscode.window.showInformationMessage(total ? `Enterprise Agent: ${total} finding(s). See the Problems panel.` : 'Enterprise Agent: no findings.');
    });
  }

  dispose(): void {
    this.scheduler.dispose();
    this.collection.dispose();
    this.output.dispose();
  }
}
