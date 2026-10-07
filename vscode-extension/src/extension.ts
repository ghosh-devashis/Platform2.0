import * as vscode from 'vscode';
import { installAssistantInstructions, installMcpConfig, registerMcpProvider } from './assistant';
import { FixProvider } from './codeActions';
import { DiagnosticsController } from './diagnostics';
import { openInvokePanel } from './invokePanel';
import { EntLensProvider } from './lenses';
import { newAgent } from './newAgent';
import { RuntimeControls } from './runtime';
import { Telemetry } from './telemetry';
import { checkExtensionUpdate } from './update';
import { checkUpgrade } from './upgrade';

export function activate(ctx: vscode.ExtensionContext): void {
  const telemetry = new Telemetry(ctx);
  const diags = new DiagnosticsController(telemetry);
  const runtime = new RuntimeControls(telemetry);
  const fixes = new FixProvider(diags, telemetry);
  const reg = (id: string, fn: (...a: never[]) => unknown) => ctx.subscriptions.push(vscode.commands.registerCommand(id, fn));
  ctx.subscriptions.push(diags, runtime);

  // EXT-02 diagnostics
  ctx.subscriptions.push(
    vscode.workspace.onDidSaveTextDocument((d) => diags.schedule(d)),
    vscode.workspace.onDidOpenTextDocument((d) => diags.schedule(d)),
    vscode.workspace.onDidCloseTextDocument((d) => diags.collection.delete(d.uri)),
  );
  vscode.workspace.textDocuments.forEach((d) => diags.schedule(d));
  reg('ent.checkWorkspace', () => diags.checkWorkspace());
  reg('ent.checkFile', async (uri?: vscode.Uri) => {
    const target = uri ?? vscode.window.activeTextEditor?.document.uri;
    if (!target) return;
    telemetry.record({ event: 'checkFile' });
    await diags.checkNow(target);
  });

  // EXT-03 quick fixes and EXT-07 lenses
  const selector: vscode.DocumentSelector = [{ language: 'python', scheme: 'file' }, { pattern: '**/agent.yaml' }, { pattern: '**/pyproject.toml' }];
  ctx.subscriptions.push(
    vscode.languages.registerCodeActionsProvider(selector, fixes, { providedCodeActionKinds: FixProvider.kinds }),
    vscode.languages.registerCodeLensProvider({ language: 'python', scheme: 'file' }, new EntLensProvider()),
  );
  reg('ent.internal.fixApplied', (rule: string) => fixes.fixApplied(rule));

  // EXT-01 new agent
  reg('ent.newAgent', () => newAgent(telemetry));

  // EXT-04 runtime controls, EXT-05 invoke panel
  reg('ent.startStack', () => runtime.start());
  reg('ent.stopStack', () => runtime.stop());
  reg('ent.openFlociUi', () => runtime.openFlociUi());
  reg('ent.openJaeger', () => runtime.openJaeger());
  reg('ent.showHealth', () => runtime.showHealth());
  reg('ent.invokePanel', () => openInvokePanel(telemetry));
  ctx.subscriptions.push(vscode.workspace.onDidChangeConfiguration((e) => e.affectsConfiguration('ent.runtime') && runtime.refreshEnabled()));

  // EXT-06 AI assistants
  const mcpRegistered = registerMcpProvider(ctx);
  reg('ent.installMcpConfig', () => installMcpConfig(telemetry));
  reg('ent.installAssistantInstructions', () => installAssistantInstructions(ctx, telemetry));
  if (!mcpRegistered) void vscode.window.showInformationMessage('This VS Code cannot register MCP servers automatically. Use "Enterprise Agent: Install MCP Standards Server Config".');

  // EXT-08 upgrade assistant, EXT-09 update checker
  reg('ent.checkUpgrade', () => checkUpgrade(ctx, telemetry, true));
  reg('ent.checkExtensionUpdate', () => checkExtensionUpdate(ctx, telemetry, true));
  setTimeout(() => {
    void checkUpgrade(ctx, telemetry, false);
    void checkExtensionUpdate(ctx, telemetry, false);
  }, 3000);

  telemetry.record({ event: 'activate' });
}

export function deactivate(): void {}
