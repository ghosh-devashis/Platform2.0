import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import type { AddressInfo } from 'node:net';
import { findOnPath, resolveAgentCommand, splitCommandLine } from '../command';
import { isCheckable, ruleDocUrl, toDiagnostic } from '../diagnostics';
import { parseFindings } from '../runner';
import { KeyedScheduler } from '../scheduler';
import { parseToolLenses } from '../lens';
import { assessUpgrade, compareVersions, detectSdkVersion, parseVersionsFile } from '../semver';
import { buildServices, parseHealth, probe, summarize, type ProbeResult } from '../health';
import { invokeAgent, jaegerTraceUrl, newSessionId } from '../invoke';
import { sanitizeEvent, toNdjsonLine } from '../telemetry';
import { feedLocation, isUpdateAvailable, parseUpdateFeed, vsixLocation } from '../feed';
import { buildMcpJson, fillStandards, findPlatformDir, mcpCommand, parseManifestIdentity, validateAgentName } from '../assistant';
import { buildInvokeHtml } from '../html';

// ---- command resolution
test('splitCommandLine handles quotes', () => {
  assert.deepEqual(splitCommandLine('uv run "my tool" --x'), ['uv', 'run', 'my tool', '--x']);
});
test('resolveAgentCommand: configured first, uv fallback second', () => {
  const c = resolveAgentCommand('ent-agent', ['check', 'a.py', '--json']);
  assert.deepEqual(c, [
    { command: 'ent-agent', args: ['check', 'a.py', '--json'] },
    { command: 'uv', args: ['run', 'ent-agent', 'check', 'a.py', '--json'] },
  ]);
  assert.equal(resolveAgentCommand('uv run ent-agent', ['mcp']).length, 1);
  assert.equal(resolveAgentCommand('', ['mcp'])[0].command, 'ent-agent');
});
test('findOnPath honours PATHEXT on Windows', () => {
  const exists = (p: string) => p === 'C:\\bin\\uv.EXE';
  assert.equal(findOnPath('uv', { PATH: 'C:\\x;C:\\bin', PATHEXT: '.COM;.EXE' }, exists, true), 'C:\\bin\\uv.EXE');
  assert.equal(findOnPath('nope', { PATH: 'C:\\bin' }, exists, true), undefined);
  assert.equal(findOnPath('uv', { PATH: '/a:/b' }, (p) => p === '/b/uv', false), '/b/uv');
});
test('mcpCommand prefers configured command, falls back to uv', () => {
  assert.deepEqual(mcpCommand('ent-agent', (c) => c === 'ent-agent'), { command: 'ent-agent', args: ['mcp'] });
  assert.deepEqual(mcpCommand('ent-agent', (c) => c === 'uv'), { command: 'uv', args: ['run', 'ent-agent', 'mcp'] });
});

// ---- diagnostics
const finding = {
  rule: 'ENT-004', severity: 'error' as const, message: 'ENT-004: Use secrets.get', path: 'a.py',
  line: 2, column: 5, end_line: 2, end_column: 33,
};
test('toDiagnostic converts 1-based to 0-based and carries code, source, link', () => {
  const d = toDiagnostic(finding, undefined, 'C:\\My Repo\\Platform2.0');
  assert.deepEqual([d.startLine, d.startCharacter, d.endLine, d.endCharacter], [1, 4, 1, 32]);
  assert.equal(d.code, 'ENT-004');
  assert.equal(d.severity, 'error');
  assert.equal(d.source, 'Enterprise Agent Toolkit');
  assert.equal(d.codeUrl, 'file:///C:/My%20Repo/Platform2.0/policy/docs/rules.md#ent-004');
});
test('toDiagnostic guards empty ranges and maps warnings', () => {
  const d = toDiagnostic({ ...finding, severity: 'warning', end_line: 2, end_column: 5 });
  assert.deepEqual([d.startCharacter, d.endCharacter], [4, 5]);
  assert.equal(d.severity, 'warning');
  assert.equal(d.codeUrl, undefined);
});
test('ruleDocUrl prefers explicit base URL and strips existing anchor', () => {
  assert.equal(ruleDocUrl('ENT-020', 'https://docs/rules.md#x', 'C:\\p'), 'https://docs/rules.md#ent-020');
});
test('isCheckable', () => {
  assert.ok(isCheckable('C:\\a\\agent.py') && isCheckable('/x/agent.yaml') && isCheckable('pyproject.toml'));
  assert.ok(!isCheckable('README.md') && !isCheckable('other.yaml'));
});
test('parseFindings tolerates leading noise and drops malformed entries', () => {
  const out = parseFindings('warning: x\n[{"rule":"ENT-001","severity":"error","message":"m","line":1,"column":1},{"bad":1}]\n');
  assert.equal(out.length, 1);
  assert.throws(() => parseFindings('nothing'));
});

// ---- scheduler
test('KeyedScheduler debounces per key and aborts the previous job', async () => {
  const calls: string[] = [];
  const signals: AbortSignal[] = [];
  const s = new KeyedScheduler(20, async (k, sig) => {
    calls.push(k);
    signals.push(sig);
    await new Promise((r) => setTimeout(r, 80));
  });
  s.request('a'); s.request('a'); s.request('b');
  await new Promise((r) => setTimeout(r, 40));
  assert.deepEqual(calls.sort(), ['a', 'b']);
  s.request('a');
  await new Promise((r) => setTimeout(r, 40));
  assert.equal(calls.filter((c) => c === 'a').length, 2);
  assert.ok(signals[0].aborted || signals[1].aborted || signals.some((x) => x.aborted));
  s.dispose();
});

// ---- tool code lens
test('parseToolLenses reads owner / classification / side effects, multi-line included', () => {
  const src = [
    'from ent_agent_sdk import tools',
    '@tools.tool(owner="crm-team", data_classification="internal")',
    'def a(): ...',
    '@tools.tool(',
    '    owner="pay", ',
    "    data_classification='restricted',",
    '    side_effects=True,',
    ')',
    'def b(): ...',
    '@tools.tool',
    'def c(): ...',
  ].join('\n');
  const l = parseToolLenses(src);
  assert.equal(l.length, 3);
  assert.deepEqual([l[0].line, l[0].owner, l[0].classification, l[0].sideEffects], [1, 'crm-team', 'internal', false]);
  assert.deepEqual([l[1].line, l[1].owner, l[1].classification, l[1].sideEffects], [3, 'pay', 'restricted', true]);
  assert.match(l[1].title, /side effects: yes/);
  assert.match(l[2].title, /owner: not set/);
});

// ---- semver / upgrade
test('compareVersions', () => {
  assert.ok(compareVersions('0.1.0', '0.2.0') < 0);
  assert.ok(compareVersions('1.10.0', '1.9.9') > 0);
  assert.equal(compareVersions('1.0', '1.0.0'), 0);
  assert.ok(compareVersions('1.0.0-rc.1', '1.0.0') < 0);
  assert.ok(compareVersions('1.0.0-rc.2', '1.0.0-rc.10') < 0);
});
test('detectSdkVersion prefers uv.lock, falls back to pyproject pin', () => {
  const lock = '[[package]]\nname = "other"\nversion = "9.9.9"\n\n[[package]]\nname = "ent-agent-sdk"\nversion = "0.1.0"\n';
  assert.deepEqual(detectSdkVersion(lock, undefined), { version: '0.1.0', source: 'uv.lock' });
  assert.deepEqual(detectSdkVersion(undefined, 'dependencies = [\n  "ent-agent-sdk~=0.1.2",\n]'), { version: '0.1.2', source: 'pyproject.toml' });
  assert.equal(detectSdkVersion(undefined, 'dependencies = []'), undefined);
});
test('parseVersionsFile handles good, bad and missing input', () => {
  const v = parseVersionsFile('{"sdk":{"latest":"0.2.0","supported":[{"version":"0.1.0","sunset":"2027-06-30"}]}}');
  assert.equal(v?.latest, '0.2.0');
  assert.equal(parseVersionsFile('{bad'), undefined);
  assert.equal(parseVersionsFile(undefined), undefined);
  assert.equal(parseVersionsFile('{"sdk":{}}'), undefined);
});
test('assessUpgrade: ok / outdated / sunset-soon / unsupported', () => {
  const info = { latest: '0.2.0', supported: [{ version: '0.1.0', sunset: '2027-06-30' }, { version: '0.0.9', sunset: '2026-01-01' }, { version: '0.1.5', sunset: '2026-11-01' }] };
  const now = new Date('2026-10-06T00:00:00Z');
  assert.equal(assessUpgrade('0.2.0', info, now).level, 'ok');
  assert.equal(assessUpgrade('0.1.0', info, now).level, 'outdated');
  assert.equal(assessUpgrade('0.1.5', info, now).level, 'sunset-soon');
  assert.equal(assessUpgrade('0.0.9', info, now).level, 'unsupported');
  assert.equal(assessUpgrade('0.3.0', info, now).level, 'ok');
});

// ---- health
test('parseHealth', () => {
  assert.equal(parseHealth(200, '{"status":"running"}').ok, true);
  assert.equal(parseHealth(200, '{"status":"unhealthy"}').ok, false);
  assert.equal(parseHealth(200, '<html>').ok, true);
  assert.deepEqual(parseHealth(503, ''), { ok: false, detail: 'HTTP 503' });
});
test('buildServices and summarize', () => {
  const s = buildServices({ flociUrl: 'http://f:1/', jaegerUrl: 'http://j', modelRouterUrl: 'http://m', invokeRouterUrl: 'http://i', agentUrls: ['http://a:8080'] });
  assert.equal(s.length, 5);
  assert.equal(s[0].url, 'http://f:1/_floci/health');
  assert.equal(s[4].url, 'http://a:8080/ping');
  const mk = (id: string, ok: boolean, required: boolean): ProbeResult => ({ id, label: id, url: '', required, ok, detail: ok ? 'ok' : 'x', ms: 1 });
  assert.equal(summarize([mk('a', true, true), mk('b', true, false)]).level, 'ok');
  assert.equal(summarize([mk('a', true, true), mk('b', false, false)]).level, 'warn');
  assert.equal(summarize([mk('a', false, true), mk('b', true, true)]).level, 'error');
  assert.match(summarize([mk('a', false, true), mk('b', false, false)]).text, /stack stopped/);
});

function serve(handler: http.RequestListener): Promise<{ url: string; close: () => void }> {
  return new Promise((resolve) => {
    const srv = http.createServer(handler).listen(0, '127.0.0.1', () => {
      resolve({ url: `http://127.0.0.1:${(srv.address() as AddressInfo).port}`, close: () => { srv.closeAllConnections(); srv.close(); } });
    });
  });
}

test('probe: up, down (HTTP 500), unreachable, timeout', async () => {
  const srv = await serve((req, res) => {
    if (req.url === '/slow') return void setTimeout(() => res.end('x'), 500);
    res.statusCode = req.url === '/bad' ? 500 : 200;
    res.end('{"status":"ok"}');
  });
  try {
    const def = (p: string) => ({ id: 'x', label: 'x', url: srv.url + p, required: true });
    assert.equal((await probe(def('/ok'))).ok, true);
    assert.equal((await probe(def('/bad'))).detail, 'HTTP 500');
    assert.equal((await probe(def('/slow'), 100)).detail, 'timeout');
    assert.equal((await probe({ id: 'x', label: 'x', url: 'http://127.0.0.1:1/', required: true }, 500)).detail, 'not reachable');
  } finally {
    srv.close();
  }
});

// ---- invoke
test('invokeAgent: success, GuardrailBlocked, agent error, network failure', async () => {
  let lastHeaders: http.IncomingHttpHeaders = {};
  let lastBody = '';
  const srv = await serve((req, res) => {
    lastHeaders = req.headers;
    let b = '';
    req.on('data', (d) => (b += d));
    req.on('end', () => {
      lastBody = b;
      const p = JSON.parse(b).prompt as string;
      res.setHeader('content-type', 'application/json');
      res.setHeader('x-ent-trace-id', 'abc123');
      if (p === 'block') { res.statusCode = 400; return void res.end('{"error":{"type":"GuardrailBlocked","message":"nope"}}'); }
      if (p === 'boom') { res.statusCode = 500; return void res.end('{"error":{"type":"AgentError","message":"failed"}}'); }
      res.end('{"output":"hi","session_id":"s1"}');
    });
  });
  try {
    const ok = await invokeAgent({ baseUrl: srv.url + '/', prompt: 'x' });
    assert.deepEqual([ok.ok, ok.status, ok.output, ok.traceId, ok.guardrailBlocked], [true, 200, 'hi', 'abc123', false]);
    assert.equal(JSON.parse(lastBody).prompt, 'x');
    assert.ok(String(lastHeaders['x-amzn-bedrock-agentcore-runtime-session-id']).length >= 33);
    const blocked = await invokeAgent({ baseUrl: srv.url, prompt: 'block', sessionId: 'my-own-session-id-that-is-long-enough-1' });
    assert.deepEqual([blocked.ok, blocked.guardrailBlocked, blocked.errorType, blocked.status], [false, true, 'GuardrailBlocked', 400]);
    assert.equal(lastHeaders['x-amzn-bedrock-agentcore-runtime-session-id'], 'my-own-session-id-that-is-long-enough-1');
    const err = await invokeAgent({ baseUrl: srv.url, prompt: 'boom' });
    assert.deepEqual([err.ok, err.guardrailBlocked, err.errorType], [false, false, 'AgentError']);
  } finally {
    srv.close();
  }
  const net = await invokeAgent({ baseUrl: 'http://127.0.0.1:1', prompt: 'x', timeoutMs: 1000 });
  assert.deepEqual([net.ok, net.status, net.errorType], [false, 0, 'NetworkError']);
});
test('session ids are >= 33 chars; jaeger link', () => {
  assert.ok(newSessionId(() => 'a').length >= 33);
  assert.equal(jaegerTraceUrl('http://localhost:16686/', 'abc'), 'http://localhost:16686/trace/abc');
});
test('invoke panel HTML has strict CSP, nonce on script/style, no inline handlers or external resources', () => {
  const html = buildInvokeHtml('NONCE123456789ab', { agentUrl: 'http://x"><script>' });
  assert.match(html, /Content-Security-Policy" content="default-src 'none'; style-src 'nonce-NONCE123456789ab'; script-src 'nonce-NONCE123456789ab';"/);
  assert.equal((html.match(/<script/g) ?? []).length, 1);
  assert.match(html, /<script nonce="NONCE123456789ab">/);
  assert.match(html, /<style nonce="NONCE123456789ab">/);
  assert.ok(!/ on[a-z]+=|style="|src="http|href="http|innerHTML/.test(html));
});

// ---- telemetry
const ID = '123e4567-e89b-42d3-a456-426614174000';
test('sanitizeEvent keeps only whitelisted fields', () => {
  const e = sanitizeEvent({ event: 'diagnostics', ruleIds: ['ENT-001', 'ENT-001', 'C:\\Users\\bob\\secret.py', 'ENT-12', 'drop table'], count: 3, path: 'C:\\x.py', prompt: 'my secret', extra: { a: 1 } }, ID, 1000);
  assert.deepEqual(e, { event: 'diagnostics', ts: 1000, installId: ID, ruleIds: ['ENT-001'], count: 3 });
  const line = toNdjsonLine(e!);
  assert.ok(line.endsWith('\n') && !/[\\/]|secret|prompt|path/.test(line.replace(ID, '')));
});
test('sanitizeEvent rejects unknown event names, free text, bad counts and bad install ids', () => {
  assert.equal(sanitizeEvent({ event: 'C:\\Users\\bob\\file.py' }, ID), undefined);
  assert.equal(sanitizeEvent({ event: 'hello world' }, ID), undefined);
  assert.equal(sanitizeEvent('x', ID), undefined);
  assert.equal(sanitizeEvent({ event: 'activate' }, 'not-a-uuid'), undefined);
  assert.equal(sanitizeEvent({ event: 'activate', count: -1 }, ID)?.count, undefined);
  assert.equal(sanitizeEvent({ event: 'activate', count: 1.5 }, ID)?.count, undefined);
  assert.equal(sanitizeEvent({ event: 'activate', count: '3' }, ID)?.count, undefined);
  assert.equal(sanitizeEvent({ event: 'activate', ruleIds: ['x/y'] }, ID)?.ruleIds, undefined);
});

// ---- update feed
test('parseUpdateFeed validates', () => {
  assert.deepEqual(parseUpdateFeed('{"latest":"0.2.0","vsix":"a-0.2.0.vsix"}'), { latest: '0.2.0', vsix: 'a-0.2.0.vsix' });
  assert.throws(() => parseUpdateFeed('nope'), /valid JSON/);
  assert.throws(() => parseUpdateFeed('{"latest":"x","vsix":"a.vsix"}'), /latest/);
  assert.throws(() => parseUpdateFeed('{"latest":"1.0.0","vsix":"../evil.exe"}'), /vsix/);
  assert.throws(() => parseUpdateFeed('{"latest":"1.0.0","vsix":"..\\\\a.vsix"}'), /vsix/);
});
test('feed location helpers and update detection', () => {
  assert.equal(feedLocation('\\\\share\\feed'), '\\\\share\\feed\\updates.json');
  assert.equal(feedLocation('https://h/feed/'), 'https://h/feed/updates.json');
  assert.equal(feedLocation('https://h/x/updates.json'), 'https://h/x/updates.json');
  assert.equal(vsixLocation('https://h/feed/updates.json', 'a.vsix'), 'https://h/feed/a.vsix');
  assert.equal(vsixLocation('C:\\feed\\updates.json', 'a.vsix'), 'C:\\feed\\a.vsix');
  assert.ok(isUpdateAvailable('0.1.0', { latest: '0.1.1', vsix: 'a.vsix' }));
  assert.ok(!isUpdateAvailable('0.1.1', { latest: '0.1.1', vsix: 'a.vsix' }));
});

// ---- assistant / new agent
test('agent name validation', () => {
  assert.equal(validateAgentName('crm-agent'), undefined);
  for (const bad of ['A', 'a', '1abc', 'Crm', 'a_b', 'a'.repeat(49), '']) assert.ok(validateAgentName(bad), bad);
  assert.equal(validateAgentName('a'.repeat(48)), undefined);
});
test('manifest identity + standards templating', () => {
  const id = parseManifestIdentity('# c\nname: crm-agent\nteam: "payments"   # t\ndata_classification: internal\n');
  assert.deepEqual(id, { name: 'crm-agent', team: 'payments' });
  assert.equal(fillStandards('# __NAME__ by __TEAM__ __NAME__', id), '# crm-agent by payments crm-agent');
  assert.equal(fillStandards('__NAME__', {}), 'this agent');
  assert.deepEqual(parseManifestIdentity(undefined), {});
});
test('buildMcpJson merges without clobbering other servers', () => {
  const out = JSON.parse(buildMcpJson('{"servers":{"other":{"command":"x"}}}', { command: 'ent-agent', args: ['mcp'] }));
  assert.equal(out.servers.other.command, 'x');
  assert.deepEqual(out.servers['ent-standards'], { type: 'stdio', command: 'ent-agent', args: ['mcp'] });
  assert.throws(() => buildMcpJson('{ // c', { command: 'a', args: [] }));
  assert.ok(JSON.parse(buildMcpJson(undefined, { command: 'a', args: [] })).servers);
});
test('findPlatformDir', () => {
  const exists = (p: string) => p === 'C:\\b\\harness\\dev.ps1';
  assert.equal(findPlatformDir('', ['C:\\a', 'C:\\b'], exists), 'C:\\b');
  assert.equal(findPlatformDir('D:\\p', ['C:\\b'], exists), 'D:\\p');
  assert.equal(findPlatformDir('', ['C:\\a'], exists), undefined);
});
