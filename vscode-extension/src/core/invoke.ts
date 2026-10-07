export interface InvokeInput {
  baseUrl: string;
  prompt: string;
  sessionId?: string;
  timeoutMs?: number;
}

export interface InvokeResult {
  ok: boolean;
  /** 0 when the request never got an HTTP response */
  status: number;
  latencyMs: number;
  output?: string;
  sessionId?: string;
  traceId?: string;
  errorType?: string;
  errorMessage?: string;
  guardrailBlocked: boolean;
  raw: string;
}

export const SESSION_HEADER = 'X-Amzn-Bedrock-AgentCore-Runtime-Session-Id';

/** A session id the runtime accepts (>= 33 chars). */
export function newSessionId(rand: () => string = () => Math.random().toString(36).slice(2) + Math.random().toString(36).slice(2)): string {
  let id = 'ent-vscode-' + rand();
  while (id.length < 33) id += rand();
  return id;
}

export function jaegerTraceUrl(jaegerBase: string, traceId: string): string {
  return jaegerBase.replace(/\/+$/, '') + '/trace/' + encodeURIComponent(traceId);
}

export async function invokeAgent(input: InvokeInput, fetchImpl: typeof fetch = fetch): Promise<InvokeResult> {
  const t0 = Date.now();
  const sessionId = input.sessionId?.trim() || newSessionId();
  try {
    const res = await fetchImpl(input.baseUrl.replace(/\/+$/, '') + '/invocations', {
      method: 'POST',
      headers: { 'content-type': 'application/json', [SESSION_HEADER]: sessionId },
      body: JSON.stringify({ prompt: input.prompt }),
      signal: AbortSignal.timeout(input.timeoutMs ?? 60000),
    });
    const raw = await res.text();
    const latencyMs = Date.now() - t0;
    const traceId = res.headers.get('x-ent-trace-id') ?? undefined;
    let json: { output?: unknown; session_id?: unknown; error?: { type?: unknown; message?: unknown } } | undefined;
    try {
      json = JSON.parse(raw);
    } catch {
      json = undefined;
    }
    const errorType = typeof json?.error?.type === 'string' ? json.error.type : res.ok ? undefined : 'HttpError';
    const errorMessage = typeof json?.error?.message === 'string' ? json.error.message : res.ok ? undefined : raw.slice(0, 300);
    return {
      ok: res.ok && !json?.error,
      status: res.status,
      latencyMs,
      output: typeof json?.output === 'string' ? json.output : undefined,
      sessionId: typeof json?.session_id === 'string' ? json.session_id : sessionId,
      traceId,
      errorType,
      errorMessage,
      guardrailBlocked: errorType === 'GuardrailBlocked',
      raw,
    };
  } catch (e) {
    const timedOut = e instanceof Error && (e.name === 'TimeoutError' || e.name === 'AbortError');
    return {
      ok: false,
      status: 0,
      latencyMs: Date.now() - t0,
      sessionId,
      errorType: timedOut ? 'Timeout' : 'NetworkError',
      errorMessage: timedOut ? 'The agent did not answer in time.' : 'Could not reach the agent. Is the local stack running?',
      guardrailBlocked: false,
      raw: '',
    };
  }
}
