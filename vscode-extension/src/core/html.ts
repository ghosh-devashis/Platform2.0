const esc = (s: string) => s.replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

export function newNonce(rand: () => string): string {
  return (rand() + rand()).replace(/[^A-Za-z0-9]/g, '').slice(0, 32).padEnd(16, 'x');
}

/**
 * HTML for the invoke panel. Strict CSP: no external resources, script and style only with the nonce,
 * no inline event handlers or style attributes. Results are rendered with textContent only.
 */
export function buildInvokeHtml(nonce: string, defaults: { agentUrl: string }): string {
  const csp = `default-src 'none'; style-src 'nonce-${nonce}'; script-src 'nonce-${nonce}';`;
  return `<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta http-equiv="Content-Security-Policy" content="${csp}">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Invoke Agent</title>
<style nonce="${nonce}">
body{font-family:var(--vscode-font-family);color:var(--vscode-foreground);padding:12px}
label{display:block;margin:10px 0 4px;font-weight:600}
input,textarea{width:100%;box-sizing:border-box;color:var(--vscode-input-foreground);background:var(--vscode-input-background);border:1px solid var(--vscode-input-border,transparent);padding:4px;font-family:var(--vscode-editor-font-family)}
textarea{min-height:90px}
button{margin-top:12px;padding:6px 14px;color:var(--vscode-button-foreground);background:var(--vscode-button-background);border:0;cursor:pointer}
button:disabled{opacity:.5;cursor:default}
.row{display:flex;gap:12px}.row>div{flex:1}
.meta{margin-top:14px;display:flex;gap:18px;flex-wrap:wrap}
.badge{padding:2px 8px;border-radius:3px;background:var(--vscode-badge-background);color:var(--vscode-badge-foreground)}
.blocked{background:var(--vscode-inputValidation-errorBackground);border:1px solid var(--vscode-inputValidation-errorBorder);padding:8px;margin-top:10px}
.hidden{display:none}
pre{white-space:pre-wrap;word-break:break-word;background:var(--vscode-textCodeBlock-background);padding:8px}
a{color:var(--vscode-textLink-foreground);cursor:pointer}
</style></head>
<body>
<h2>Invoke local agent</h2>
<div class="row">
<div><label for="url">Agent URL</label><input id="url" value="${esc(defaults.agentUrl)}"></div>
<div><label for="session">Session id (optional, 33+ chars)</label><input id="session" placeholder="generated if empty"></div>
</div>
<label for="prompt">Prompt</label><textarea id="prompt">Hello</textarea>
<button id="send">Send</button>
<div id="result" class="hidden">
<div class="meta">
<span>Status: <span class="badge" id="status"></span></span>
<span>Latency: <span class="badge" id="latency"></span></span>
<span>Guardrail: <span class="badge" id="guardrail"></span></span>
<span>Trace: <a id="trace" class="hidden"></a><span id="notrace">none</span></span>
</div>
<div id="blocked" class="blocked hidden"></div>
<h3 id="outlabel">Response</h3>
<pre id="output"></pre>
</div>
<script nonce="${nonce}">
(function () {
  const vscode = acquireVsCodeApi();
  const $ = (id) => document.getElementById(id);
  let traceId = '';
  $('send').addEventListener('click', () => {
    $('send').disabled = true;
    vscode.postMessage({ type: 'invoke', url: $('url').value, prompt: $('prompt').value, sessionId: $('session').value });
  });
  $('trace').addEventListener('click', () => vscode.postMessage({ type: 'openTrace', traceId }));
  window.addEventListener('message', (ev) => {
    const m = ev.data;
    if (!m || m.type !== 'result') return;
    const r = m.result;
    $('send').disabled = false;
    $('result').classList.remove('hidden');
    $('status').textContent = r.status ? String(r.status) : 'no response';
    $('latency').textContent = r.latencyMs + ' ms';
    $('guardrail').textContent = r.guardrailBlocked ? 'BLOCKED' : 'passed';
    $('blocked').classList.toggle('hidden', !r.guardrailBlocked);
    $('blocked').textContent = r.guardrailBlocked ? 'Blocked by guardrail: ' + (r.errorMessage || '') : '';
    traceId = r.traceId || '';
    $('trace').classList.toggle('hidden', !traceId);
    $('notrace').classList.toggle('hidden', !!traceId);
    $('trace').textContent = traceId;
    $('outlabel').textContent = r.ok ? 'Response' : 'Error' + (r.errorType ? ' (' + r.errorType + ')' : '');
    $('output').textContent = r.ok ? (r.output ?? r.raw) : (r.errorMessage || r.raw || '');
  });
})();
</script>
</body></html>`;
}
