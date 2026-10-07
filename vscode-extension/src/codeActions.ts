import * as vscode from 'vscode';
import { fixToEdits } from './core/fix';
import { SOURCE } from './core/diagnostics';
import type { DiagnosticsController } from './diagnostics';
import type { Telemetry } from './telemetry';

export class FixProvider implements vscode.CodeActionProvider {
  static readonly kinds = [vscode.CodeActionKind.QuickFix];

  constructor(private readonly diags: DiagnosticsController, private readonly telemetry: Telemetry) {}

  provideCodeActions(doc: vscode.TextDocument, _range: vscode.Range, ctx: vscode.CodeActionContext): vscode.CodeAction[] {
    const findings = this.diags.findingsFor(doc.uri);
    const text = doc.getText();
    const actions: vscode.CodeAction[] = [];
    for (const d of ctx.diagnostics) {
      if (d.source !== SOURCE) continue;
      const code = typeof d.code === 'object' ? String(d.code.value) : String(d.code);
      const f = findings.find((x) => x.rule === code && x.line - 1 === d.range.start.line && x.column - 1 === d.range.start.character);
      if (!f?.fix) continue;
      const action = new vscode.CodeAction(f.fix.description, vscode.CodeActionKind.QuickFix);
      action.diagnostics = [d];
      action.isPreferred = true;
      const edit = new vscode.WorkspaceEdit();
      for (const e of fixToEdits(text, f.fix)) edit.replace(doc.uri, new vscode.Range(e.line, e.character, e.endLine, e.endCharacter), e.newText);
      action.edit = edit;
      action.command = { command: 'ent.internal.fixApplied', title: 'fix applied', arguments: [code] };
      actions.push(action);
    }
    return actions;
  }

  /** Telemetry hook; the fix itself is applied by the WorkspaceEdit. */
  fixApplied(rule: string): void {
    this.telemetry.record({ event: 'quickFix', ruleIds: [rule], count: 1 });
  }
}
