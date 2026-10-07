import * as vscode from 'vscode';
import * as path from 'node:path';
import { parseToolLenses } from './core/lens';

export class EntLensProvider implements vscode.CodeLensProvider {
  provideCodeLenses(doc: vscode.TextDocument): vscode.CodeLens[] {
    const lenses: vscode.CodeLens[] = [];
    if (path.basename(doc.fileName) === 'agent.py') {
      lenses.push(new vscode.CodeLens(new vscode.Range(0, 0, 0, 0), { title: '$(shield) Run Enterprise checks', command: 'ent.checkFile', arguments: [doc.uri] }));
    }
    for (const t of parseToolLenses(doc.getText())) {
      lenses.push(new vscode.CodeLens(new vscode.Range(t.line, 0, t.line, 0), { title: t.title, command: '' }));
    }
    return lenses;
  }
}
