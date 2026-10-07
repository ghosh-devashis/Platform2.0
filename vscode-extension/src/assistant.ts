import * as vscode from 'vscode';
import * as fs from 'node:fs';
import * as path from 'node:path';
import { agentCommand, readText } from './config';
import { buildMcpJson, fillStandards, INSTRUCTION_FILES, mcpCommand, parseManifestIdentity } from './core/assistant';
import { findOnPath } from './core/command';
import type { Telemetry } from './telemetry';

const commandExists = (cmd: string) => !!findOnPath(cmd, process.env, (p) => fs.existsSync(p));
const firstFolder = () => vscode.workspace.workspaceFolders?.[0];

/** Register the MCP standards server with VS Code (needs the MCP provider API, VS Code 1.101+). Returns false if unavailable. */
export function registerMcpProvider(ctx: vscode.ExtensionContext): boolean {
  const lm = vscode.lm as Partial<typeof vscode.lm>;
  if (typeof lm.registerMcpServerDefinitionProvider !== 'function') return false;
  const changed = new vscode.EventEmitter<void>();
  ctx.subscriptions.push(
    changed,
    vscode.workspace.onDidChangeConfiguration((e) => e.affectsConfiguration('ent.agentCommand') && changed.fire()),
    lm.registerMcpServerDefinitionProvider('ent.standards', {
      onDidChangeMcpServerDefinitions: changed.event,
      provideMcpServerDefinitions: () => {
        const spec = mcpCommand(agentCommand(), commandExists);
        const def = new vscode.McpStdioServerDefinition('Enterprise Agent Standards', spec.command, spec.args, {}, String(ctx.extension.packageJSON.version));
        def.cwd = firstFolder()?.uri;
        return [def];
      },
    }),
  );
  return true;
}

/** Fallback / explicit: write .vscode/mcp.json. */
export async function installMcpConfig(telemetry: Telemetry): Promise<void> {
  const folder = firstFolder();
  if (!folder) return void vscode.window.showInformationMessage('Open a folder first.');
  const file = path.join(folder.uri.fsPath, '.vscode', 'mcp.json');
  try {
    const out = buildMcpJson(readText(file), mcpCommand(agentCommand(), commandExists));
    fs.mkdirSync(path.dirname(file), { recursive: true });
    fs.writeFileSync(file, out);
    telemetry.record({ event: 'installMcpConfig' });
    const doc = await vscode.workspace.openTextDocument(file);
    await vscode.window.showTextDocument(doc);
    void vscode.window.showInformationMessage('Added the "ent-standards" MCP server to .vscode/mcp.json. Start it from the MCP: List Servers command.');
  } catch (e) {
    void vscode.window.showErrorMessage(String(e instanceof Error ? e.message : e));
  }
}

/** Copy the standards into CLAUDE.md, AGENTS.md and .github/copilot-instructions.md if absent. */
export function installAssistantInstructions(ctx: vscode.ExtensionContext, telemetry: Telemetry): void {
  const folder = firstFolder();
  if (!folder) return void vscode.window.showInformationMessage('Open a folder first.');
  const template = readText(path.join(ctx.extensionPath, 'resources', 'standards.md'));
  if (!template) return void vscode.window.showErrorMessage('Standards text is missing from the extension package.');
  const content = fillStandards(template, parseManifestIdentity(readText(path.join(folder.uri.fsPath, 'agent.yaml'))));
  const created: string[] = [];
  const skipped: string[] = [];
  for (const rel of INSTRUCTION_FILES) {
    const target = path.join(folder.uri.fsPath, ...rel.split('/'));
    if (fs.existsSync(target)) {
      skipped.push(rel);
      continue;
    }
    fs.mkdirSync(path.dirname(target), { recursive: true });
    fs.writeFileSync(target, content);
    created.push(rel);
  }
  telemetry.record({ event: 'installAssistantInstructions', count: created.length });
  void vscode.window.showInformationMessage(
    `Assistant instructions: created ${created.join(', ') || 'nothing'}${skipped.length ? `; kept existing ${skipped.join(', ')}` : ''}.`,
  );
}
