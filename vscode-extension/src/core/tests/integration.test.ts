import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, writeFileSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { spawnSync } from 'node:child_process';
import { runCheck } from '../runner';
import { toDiagnostic } from '../diagnostics';
import { applyFix } from '../fix';

const hasUv = spawnSync('uv', ['--version'], { shell: false }).status === 0;
// repo root = vscode-extension/../ (out/core -> up three levels)
const repoRoot = resolve(__dirname, '..', '..', '..', '..');

test('integration: ent-agent check via runCheck maps to a correct diagnostic and fix', { skip: hasUv ? false : 'uv not on PATH' }, async () => {
  const dir = mkdtempSync(join(tmpdir(), 'ent-ext-'));
  try {
    const file = join(dir, 'a.py');
    const src = 'import os\nk = os.environ["OPENAI_API_KEY"]\n';
    writeFileSync(file, src);
    // Use the uv fallback explicitly (also exercises the ENOENT fallback: the first command does not exist).
    const findings = await runCheck('definitely-not-ent-agent', [file], { cwd: repoRoot, timeoutMs: 120000 });
    const f = findings.find((x) => x.rule === 'ENT-004');
    assert.ok(f, 'ENT-004 expected');
    const d = toDiagnostic(f);
    assert.deepEqual([d.startLine, d.startCharacter, d.endLine, d.endCharacter], [1, 4, 1, 32]);
    assert.equal(d.code, 'ENT-004');
    assert.equal(d.severity, 'error');
    assert.ok(f.fix);
    const fixed = applyFix(readFileSync(file, 'utf8'), f.fix);
    assert.equal(fixed, 'import os\nfrom ent_agent_sdk import secrets\nk = secrets.get("openai-api-key")\n');
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});
