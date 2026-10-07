import test from 'node:test';
import assert from 'node:assert/strict';
import { applyFix, findImportInsertLine, fixToEdits, hasImport } from '../fix';
import type { Fix } from '../types';

const fix: Fix = {
  description: 'Replace with secrets.get("k")',
  edits: [{ line: 2, column: 5, end_line: 2, end_column: 33, new_text: 'secrets.get("k")' }],
  imports: ['from ent_agent_sdk import secrets'],
};

test('applyFix replaces the 1-based range and adds the import after the last import', () => {
  const src = 'import os\nk = os.environ["OPENAI_API_KEY"]\n';
  assert.equal(applyFix(src, fix), 'import os\nfrom ent_agent_sdk import secrets\nk = secrets.get("k")\n');
});

test('applyFix is idempotent for imports already present', () => {
  const src = 'import os\nfrom ent_agent_sdk import secrets\nk = os.environ["OPENAI_API_KEY"]\n';
  const f: Fix = { ...fix, edits: [{ ...fix.edits[0], line: 3, end_line: 3 }] };
  assert.equal(applyFix(src, f), 'import os\nfrom ent_agent_sdk import secrets\nk = secrets.get("k")\n');
  assert.equal(fixToEdits(src, f).length, 1);
});

test('existing from-import with more names satisfies the import (incl. parenthesised)', () => {
  assert.ok(hasImport(['from ent_agent_sdk import (', '    secrets,', '    tools,', ')', 'x = 1'], 'from ent_agent_sdk import secrets'));
  assert.ok(hasImport(['from ent_agent_sdk import secrets, tools'], 'from ent_agent_sdk import tools'));
  assert.ok(!hasImport(['from ent_agent_sdk import tools'], 'from ent_agent_sdk import secrets'));
  assert.ok(!hasImport(['import os'], 'import sys'));
});

test('import goes after a docstring when there are no imports', () => {
  assert.equal(findImportInsertLine(['"""Doc', 'more"""', 'x = 1']), 2);
  assert.equal(findImportInsertLine(['"""One line."""', '', 'x = 1']), 1);
  assert.equal(findImportInsertLine(['x = 1']), 0);
});

test('import goes after the last top-level import, including multi-line ones, and skips later code', () => {
  const lines = ['"""d"""', 'from __future__ import annotations', '', 'from a import (', '    b,', ')', 'import c', '', 'def f():', '    import inner', ''];
  assert.equal(findImportInsertLine(lines), 7);
});

test('multiple edits are applied bottom-up so earlier offsets stay valid; CRLF preserved', () => {
  const src = 'import os\r\na = 1\r\nb = 2\r\n';
  const f: Fix = {
    description: 'x',
    edits: [
      { line: 2, column: 5, end_line: 2, end_column: 6, new_text: '10' },
      { line: 3, column: 5, end_line: 3, end_column: 6, new_text: '20' },
    ],
    imports: ['import sys'],
  };
  assert.equal(applyFix(src, f), 'import os\r\nimport sys\r\na = 10\r\nb = 20\r\n');
});

test('multi-line edit range', () => {
  const src = 'x = foo(\n    1,\n)\ny = 2\n';
  const f: Fix = { description: 'x', edits: [{ line: 1, column: 5, end_line: 3, end_column: 2, new_text: 'bar()' }] };
  assert.equal(applyFix(src, f), 'x = bar()\ny = 2\n');
});

test('import is appended when file has no trailing newline and only imports', () => {
  const f: Fix = { description: 'x', edits: [], imports: ['import sys'] };
  assert.equal(applyFix('import os', f), 'import os\nimport sys\n');
});

test('fixToEdits yields 0-based coordinates against the original text', () => {
  const src = 'import os\nk = os.environ["A"]\n';
  const edits = fixToEdits(src, fix);
  assert.deepEqual(edits[0], { line: 1, character: 4, endLine: 1, endCharacter: 32, newText: 'secrets.get("k")' });
  assert.deepEqual(edits[1], { line: 1, character: 0, endLine: 1, endCharacter: 0, newText: 'from ent_agent_sdk import secrets\n' });
});
