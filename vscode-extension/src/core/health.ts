export interface ServiceDef {
  id: string;
  label: string;
  url: string;
  /** Required services turn the status bar red when down; optional ones (agents) only warn. */
  required: boolean;
}

export interface ProbeResult {
  id: string;
  label: string;
  url: string;
  required: boolean;
  ok: boolean;
  detail: string;
  ms: number;
}

export interface RuntimeConfig {
  flociUrl: string;
  jaegerUrl: string;
  modelRouterUrl: string;
  invokeRouterUrl: string;
  agentUrls: string[];
}

const trim = (u: string) => u.replace(/\/+$/, '');

export function buildServices(c: RuntimeConfig): ServiceDef[] {
  return [
    { id: 'floci', label: 'Floci (AWS emulator)', url: trim(c.flociUrl) + '/_floci/health', required: true },
    { id: 'jaeger', label: 'Jaeger (traces)', url: trim(c.jaegerUrl) + '/', required: true },
    { id: 'model-router', label: 'Model router', url: trim(c.modelRouterUrl) + '/health', required: true },
    { id: 'invoke-router', label: 'Invoke router', url: trim(c.invokeRouterUrl) + '/_router/health', required: true },
    ...c.agentUrls.map((u, i) => ({ id: `agent-${i}`, label: `Agent ${trim(u).replace(/^https?:\/\//, '')}`, url: trim(u) + '/ping', required: false })),
  ];
}

/** Decide health from an HTTP status and body: 2xx, and no JSON status field saying otherwise. */
export function parseHealth(status: number, body: string): { ok: boolean; detail: string } {
  if (status < 200 || status >= 300) return { ok: false, detail: `HTTP ${status}` };
  try {
    const j = JSON.parse(body) as { status?: unknown };
    if (j && typeof j.status === 'string') {
      const bad = /^(down|unhealthy|error|fail|failed|degraded)/i.test(j.status);
      return { ok: !bad, detail: j.status };
    }
  } catch {
    // not JSON: a 2xx is enough
  }
  return { ok: true, detail: 'ok' };
}

export async function probe(def: ServiceDef, timeoutMs = 2000, fetchImpl: typeof fetch = fetch): Promise<ProbeResult> {
  const t0 = Date.now();
  const base = { id: def.id, label: def.label, url: def.url, required: def.required };
  try {
    const res = await fetchImpl(def.url, { signal: AbortSignal.timeout(timeoutMs) });
    const body = await res.text();
    const h = parseHealth(res.status, body.slice(0, 4096));
    return { ...base, ...h, ms: Date.now() - t0 };
  } catch (e) {
    const timedOut = e instanceof Error && (e.name === 'TimeoutError' || e.name === 'AbortError');
    return { ...base, ok: false, detail: timedOut ? 'timeout' : 'not reachable', ms: Date.now() - t0 };
  }
}

export interface Summary {
  text: string;
  level: 'ok' | 'warn' | 'error';
  tooltip: string;
}

export function summarize(results: ProbeResult[]): Summary {
  const infra = results.filter((r) => r.required);
  const agents = results.filter((r) => !r.required);
  const infraUp = infra.filter((r) => r.ok).length;
  const agentsUp = agents.filter((r) => r.ok).length;
  const level = infraUp < infra.length ? 'error' : agents.length && agentsUp === 0 ? 'warn' : 'ok';
  const icon = level === 'ok' ? '$(check)' : level === 'warn' ? '$(warning)' : '$(error)';
  const agentText = agents.length ? `, agents ${agentsUp}/${agents.length}` : '';
  const text = infraUp === 0 && agentsUp === 0 ? `${icon} Ent: stack stopped` : `${icon} Ent: ${infraUp}/${infra.length}${agentText}`;
  const tooltip = results.map((r) => `${r.ok ? 'up  ' : 'DOWN'} ${r.label} (${r.detail})`).join('\n');
  return { text, level, tooltip };
}
