import type { Fix, TextEdit0 } from './types';

interface ImportStmt {
  start: number;
  end: number;
  text: string;
}

/** Line index just past a top-level docstring starting at `i`, or `i` if there is none. */
function skipDocstring(lines: string[], i: number): number {
  const m = /^[rRuUbBfF]{0,2}("""|''')/.exec(lines[i] ?? '');
  if (m) {
    const q = m[1];
    const rest = lines[i].slice(m[0].length);
    if (rest.includes(q)) return i + 1;
    for (let j = i + 1; j < lines.length; j++) if (lines[j].includes(q)) return j + 1;
    return lines.length;
  }
  const s = /^[rRuUbBfF]{0,2}("[^"]*"|'[^']*')\s*(#.*)?$/.exec(lines[i] ?? '');
  return s ? i + 1 : i;
}

/** Scan the top-of-file region: where a docstring ends and the top-level import statements. */
function scanHeader(lines: string[]): { afterDoc: number; imports: ImportStmt[] } {
  let i = 0;
  while (i < lines.length && (lines[i].trim() === '' || lines[i].startsWith('#'))) i++;
  const afterDoc = skipDocstring(lines, i);
  const imports: ImportStmt[] = [];
  let j = afterDoc;
  while (j < lines.length) {
    const line = lines[j];
    if (line.trim() === '' || line.startsWith('#')) {
      j++;
      continue;
    }
    if (!/^(import|from)\s/.test(line)) break;
    let end = j;
    let depth = 0;
    let text = '';
    for (;;) {
      const l = lines[end];
      text += ' ' + l.replace(/#.*$/, '');
      for (const ch of l.replace(/#.*$/, '')) {
        if (ch === '(') depth++;
        else if (ch === ')') depth--;
      }
      const cont = /\\\s*$/.test(l);
      if (depth <= 0 && !cont) break;
      end++;
      if (end >= lines.length) {
        end = lines.length - 1;
        break;
      }
    }
    imports.push({ start: j, end, text: text.replace(/\\\s*$/gm, ' ').replace(/[()]/g, ' ').replace(/\s+/g, ' ').trim() });
    j = end + 1;
  }
  return { afterDoc, imports };
}

/** 0-based line before which new imports go: after the last top-level import, else after the docstring, else line 0. */
export function findImportInsertLine(lines: string[]): number {
  const { afterDoc, imports } = scanHeader(lines);
  if (imports.length) return imports[imports.length - 1].end + 1;
  return afterDoc;
}

function parseFrom(t: string): { module: string; names: string[] } | undefined {
  const m = /^from\s+(\S+)\s+import\s+(.+)$/.exec(t);
  if (!m) return undefined;
  return { module: m[1], names: m[2].split(',').map((n) => n.trim()).filter(Boolean) };
}

/** True when the import statement is already satisfied by an existing top-level import. */
export function hasImport(lines: string[], imp: string): boolean {
  const norm = imp.replace(/[()]/g, ' ').replace(/\s+/g, ' ').trim();
  const want = parseFrom(norm);
  for (const s of scanHeader(lines).imports) {
    if (s.text === norm) return true;
    if (want) {
      const have = parseFrom(s.text);
      if (have && have.module === want.module && (have.names.includes('*') || want.names.every((n) => have.names.includes(n)))) return true;
    }
  }
  return false;
}

/** All edits for a fix as 0-based edits against the ORIGINAL text (safe to hand to VS Code in one WorkspaceEdit). */
export function fixToEdits(text: string, fix: Fix): TextEdit0[] {
  const eol = text.includes('\r\n') ? '\r\n' : '\n';
  const lines = text.split(/\r?\n/);
  const edits: TextEdit0[] = fix.edits.map((e) => ({
    line: e.line - 1,
    character: e.column - 1,
    endLine: e.end_line - 1,
    endCharacter: e.end_column - 1,
    newText: e.new_text,
  }));
  const missing = (fix.imports ?? []).filter((imp, i, all) => all.indexOf(imp) === i && !hasImport(lines, imp));
  if (missing.length) {
    const at = findImportInsertLine(lines);
    // Inserting at the end of the file when it lacks a trailing newline needs a leading separator.
    const atEnd = at >= lines.length;
    const body = missing.join(eol);
    edits.push({
      line: atEnd ? lines.length - 1 : at,
      character: atEnd ? lines[lines.length - 1].length : 0,
      endLine: atEnd ? lines.length - 1 : at,
      endCharacter: atEnd ? lines[lines.length - 1].length : 0,
      newText: atEnd ? eol + body + eol : body + eol,
    });
  }
  return edits;
}

function lineStarts(text: string): number[] {
  const starts = [0];
  for (let i = 0; i < text.length; i++) if (text[i] === '\n') starts.push(i + 1);
  return starts;
}

/** Apply non-overlapping 0-based edits (bottom-up so earlier offsets stay valid). */
export function applyEdits(text: string, edits: TextEdit0[]): string {
  const starts = lineStarts(text);
  const off = (line: number, ch: number) => Math.min(text.length, (starts[Math.min(line, starts.length - 1)] ?? text.length) + ch);
  const withOffsets = edits.map((e, idx) => ({ s: off(e.line, e.character), e: off(e.endLine, e.endCharacter), t: e.newText, idx }));
  withOffsets.sort((a, b) => b.s - a.s || b.idx - a.idx);
  let out = text;
  for (const w of withOffsets) out = out.slice(0, w.s) + w.t + out.slice(w.e);
  return out;
}

/** Apply a fix (edits + imports) to a whole document. Idempotent for imports. */
export function applyFix(text: string, fix: Fix): string {
  return applyEdits(text, fixToEdits(text, fix));
}
