/**
 * Per-invocation context: who is calling which agent, in which session, under which guardrails.
 *
 * Set by `EnterpriseAgentApp` for the duration of each invocation (AsyncLocalStorage). Audit events, tool wrappers
 * and guardrails read it, so agent code never has to pass identity or policy around.
 */

import { AsyncLocalStorage } from "node:async_hooks";
import type { Guardrails } from "./guardrails.js";

export interface InvocationContext {
  agentName: string;
  agentVersion: string;
  team?: string | null;
  sessionId?: string | null;
  userId?: string | null;
  traceId?: string | null;
  guardrails?: Guardrails | null;
  /** Token budget tracker of this invocation (budget.ts). */
  budget?: unknown;
  /** From the manifest: public | internal | confidential | restricted. */
  dataClassification?: string | null;
}

const storage = new AsyncLocalStorage<InvocationContext>();

/** The context of the invocation being served, or undefined outside one. */
export function current(): InvocationContext | undefined {
  return storage.getStore();
}

/** Run `fn` with `invocation` as the current context. */
export function use<T>(invocation: InvocationContext, fn: () => T): T {
  return storage.run(invocation, fn);
}
