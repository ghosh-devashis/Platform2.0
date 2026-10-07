import * as vscode from 'vscode';
import * as fs from 'node:fs';
import * as path from 'node:path';
import { randomUUID } from 'node:crypto';
import { cfg } from './config';
import { isValidInstallId, sanitizeEvent, toNdjsonLine } from './core/telemetry';

/** Opt-in, anonymised telemetry. Off unless `ent.telemetry.enabled` is true. Only whitelisted fields leave this class. */
export class Telemetry {
  constructor(private readonly ctx: vscode.ExtensionContext) {}

  private installId(): string {
    let id = this.ctx.globalState.get<string>('ent.installId');
    if (!isValidInstallId(id)) {
      id = randomUUID();
      void this.ctx.globalState.update('ent.installId', id);
    }
    return id;
  }

  record(raw: { event: string; ruleIds?: string[]; count?: number }): void {
    if (!cfg().get<boolean>('telemetry.enabled', false)) return;
    const e = sanitizeEvent(raw, this.installId());
    if (!e) return;
    try {
      const dir = this.ctx.globalStorageUri.fsPath;
      fs.mkdirSync(dir, { recursive: true });
      fs.appendFileSync(path.join(dir, 'telemetry.ndjson'), toNdjsonLine(e));
    } catch {
      // local logging is best effort
    }
    const endpoint = cfg().get<string>('telemetry.endpoint', '').trim();
    if (/^https?:\/\//i.test(endpoint)) {
      fetch(endpoint, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(e), signal: AbortSignal.timeout(5000) }).catch(() => undefined);
    }
  }
}
