import * as vscode from 'vscode';
import * as path from 'node:path';
import { agentCommand, cwdFor } from './config';
import { CLASSIFICATIONS, newAgentArgs, validateAgentName, validateTeam } from './core/assistant';
import { runCli } from './core/runner';
import type { Telemetry } from './telemetry';

export async function newAgent(telemetry: Telemetry): Promise<void> {
  const name = await vscode.window.showInputBox({ title: 'New Agent (1/4): name', prompt: 'Lowercase letters, digits and hyphens', validateInput: (v) => validateAgentName(v) });
  if (!name) return;
  const team = await vscode.window.showInputBox({ title: 'New Agent (2/4): owning team', validateInput: (v) => validateTeam(v) });
  if (!team) return;
  const classification = await vscode.window.showQuickPick(
    CLASSIFICATIONS.map((c) => ({ label: c, description: c === 'restricted' ? 'uses the strict guardrail profile' : undefined })),
    { title: 'New Agent (3/4): data classification', placeHolder: 'Highest sensitivity of data the agent handles' },
  );
  if (!classification) return;
  const folders = await vscode.window.showOpenDialog({ title: 'New Agent (4/4): parent folder', canSelectFiles: false, canSelectFolders: true, canSelectMany: false, openLabel: 'Create agent here' });
  if (!folders?.length) return;
  const parent = folders[0].fsPath;
  try {
    const r = await vscode.window.withProgress({ location: vscode.ProgressLocation.Notification, title: `Creating ${name}...` }, () =>
      runCli(agentCommand(), newAgentArgs(name, team, classification.label, parent), { cwd: cwdFor() ?? parent, timeoutMs: 120000 }),
    );
    if (r.code !== 0) throw new Error((r.stderr || r.stdout).trim().slice(0, 600) || `exit code ${r.code}`);
    telemetry.record({ event: 'newAgent' });
    const choice = await vscode.window.showInformationMessage(`Created ${name}.`, 'Open folder', 'Open in new window');
    if (choice) await vscode.commands.executeCommand('vscode.openFolder', vscode.Uri.file(path.join(parent, name)), { forceNewWindow: choice === 'Open in new window' });
  } catch (e) {
    void vscode.window.showErrorMessage(`Could not create the agent: ${e instanceof Error ? e.message : String(e)}`);
  }
}
