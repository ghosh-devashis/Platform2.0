/**
 * Telemetry events are built ONLY from this whitelist. Anything else (free text, paths, code,
 * prompts, unknown keys) is dropped or the whole event is rejected.
 */
export const EVENT_NAMES = [
  'activate',
  'newAgent',
  'checkFile',
  'checkWorkspace',
  'diagnostics',
  'quickFix',
  'startStack',
  'stopStack',
  'invoke',
  'installMcpConfig',
  'installAssistantInstructions',
  'upgradePrompt',
  'upgradeAccepted',
  'updateOffered',
] as const;

export interface TelemetryEvent {
  event: (typeof EVENT_NAMES)[number];
  ruleIds?: string[];
  count?: number;
  ts: number;
  installId: string;
}

const RULE_ID = /^ENT-\d{3}$/;
const INSTALL_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

/** Validate untrusted input and return a clean event, or undefined if it cannot be made safe. */
export function sanitizeEvent(raw: unknown, installId: string, now: number = Date.now()): TelemetryEvent | undefined {
  if (!raw || typeof raw !== 'object') return undefined;
  const r = raw as Record<string, unknown>;
  if (typeof r.event !== 'string' || !(EVENT_NAMES as readonly string[]).includes(r.event)) return undefined;
  if (!INSTALL_ID.test(installId)) return undefined;
  const out: TelemetryEvent = { event: r.event as TelemetryEvent['event'], ts: now, installId };
  if (Array.isArray(r.ruleIds)) {
    const ids = [...new Set(r.ruleIds.filter((x): x is string => typeof x === 'string' && RULE_ID.test(x)))].slice(0, 50);
    if (ids.length) out.ruleIds = ids;
  }
  if (typeof r.count === 'number' && Number.isInteger(r.count) && r.count >= 0 && r.count <= 1_000_000) out.count = r.count;
  return out;
}

export function toNdjsonLine(e: TelemetryEvent): string {
  return JSON.stringify(e) + '\n';
}

export function isValidInstallId(id: unknown): id is string {
  return typeof id === 'string' && INSTALL_ID.test(id);
}
