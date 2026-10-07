import * as vscode from 'vscode';
import { randomBytes } from 'node:crypto';
import { cfg, runtimeConfig } from './config';
import { buildInvokeHtml, newNonce } from './core/html';
import { invokeAgent, jaegerTraceUrl } from './core/invoke';
import type { Telemetry } from './telemetry';

let panel: vscode.WebviewPanel | undefined;

export function openInvokePanel(telemetry: Telemetry): void {
  if (panel) {
    panel.reveal();
    return;
  }
  panel = vscode.window.createWebviewPanel('entInvoke', 'Invoke Agent', vscode.ViewColumn.Beside, { enableScripts: true, localResourceRoots: [] });
  panel.onDidDispose(() => (panel = undefined));
  const nonce = newNonce(() => randomBytes(16).toString('base64'));
  panel.webview.html = buildInvokeHtml(nonce, { agentUrl: runtimeConfig().agentUrls[0] ?? 'http://localhost:8080' });
  const wv = panel.webview;
  wv.onDidReceiveMessage(async (m: { type?: string; url?: string; prompt?: string; sessionId?: string; traceId?: string }) => {
    if (m.type === 'invoke' && typeof m.url === 'string' && typeof m.prompt === 'string') {
      if (!/^https?:\/\/(localhost|127\.0\.0\.1|\[::1\])(:\d+)?(\/|$)/i.test(m.url)) {
        void vscode.window.showWarningMessage('The invoke panel only talks to local agents (localhost).');
        void wv.postMessage({ type: 'result', result: { ok: false, status: 0, latencyMs: 0, guardrailBlocked: false, errorType: 'Rejected', errorMessage: 'Only localhost URLs are allowed.', raw: '' } });
        return;
      }
      const result = await invokeAgent({ baseUrl: m.url, prompt: m.prompt, sessionId: typeof m.sessionId === 'string' ? m.sessionId : undefined });
      telemetry.record({ event: 'invoke' });
      void wv.postMessage({ type: 'result', result });
    } else if (m.type === 'openTrace' && typeof m.traceId === 'string' && /^[0-9a-zA-Z-]{8,64}$/.test(m.traceId)) {
      void vscode.env.openExternal(vscode.Uri.parse(jaegerTraceUrl(cfg().get<string>('runtime.jaegerUrl', 'http://localhost:16686'), m.traceId)));
    }
  });
}
