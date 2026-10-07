/**
 * Audit events (SDK-13): a dedicated, structured stream for security-relevant actions.
 *
 * Events: tool calls with side effects, guardrail blocks and redactions, waivers in effect. Each event carries the
 * agent and user identity and the trace ID, so it can be joined to traces and logs.
 *
 * Audit events never contain prompt, response or tool content: only names, categories, counts and outcomes.
 *
 * The stream is separate from application logs: one JSON object per line on stdout, or in the file named by
 * `ENT_AUDIT_LOG_PATH`. Use `addSink()` to forward events elsewhere (a queue, a SIEM). Audit is best effort and
 * never fails an invocation (a failing sink is logged and skipped).
 */

import { appendFileSync } from "node:fs";
import { trace, type AttributeValue } from "@opentelemetry/api";
import * as context from "./context.js";
import { getLogger } from "./log.js";

const logger = getLogger("ent_agent_sdk.audit");

export const TOOL_CALL = "tool_call";
export const GUARDRAIL_BLOCK = "guardrail_block";
export const GUARDRAIL_REDACT = "guardrail_redact";
export const WAIVER_IN_EFFECT = "waiver_in_effect";
export const BUDGET_SOFT = "budget_soft_limit";
export const BUDGET_EXCEEDED = "budget_exceeded";

export interface AuditEvent {
  timestamp: string;
  stream: "audit";
  event_type: string;
  outcome: string;
  agent: string | null | undefined;
  agent_version: string | null;
  team: string | null;
  user_id: string | null;
  session_id: string | null;
  trace_id: string | null;
  details: Record<string, unknown>;
}

export type AuditSink = (event: AuditEvent) => void;

const sinks: AuditSink[] = [];
let filePath: string | undefined;
let configured = false;

/** Send audit events to their own stream (file from `ENT_AUDIT_LOG_PATH`, else stdout). Idempotent. */
export function configure(path?: string): void {
  if (configured) return;
  filePath = path ?? process.env.ENT_AUDIT_LOG_PATH;
  configured = true;
}

/** For tests: forget configuration and sinks. */
export function reset(): void {
  configured = false;
  filePath = undefined;
  sinks.length = 0;
}

/** Also deliver every audit event to `sink(event)`. */
export function addSink(sink: AuditSink): void {
  sinks.push(sink);
}

export function removeSink(sink: AuditSink): void {
  const i = sinks.indexOf(sink);
  if (i >= 0) sinks.splice(i, 1);
}

/** Python-compatible ISO timestamp with millisecond precision and `+00:00` offset. */
function isoNow(): string {
  return new Date().toISOString().replace("Z", "+00:00");
}

/** Record one audit event and return it. Pass only names, categories, counts, never content. */
export function emit(
  eventType: string,
  fields: { outcome?: string; [detail: string]: unknown } = {},
): AuditEvent {
  const { outcome = "success", ...details } = fields;
  const invocation = context.current();
  const event: AuditEvent = {
    timestamp: isoNow(),
    stream: "audit",
    event_type: eventType,
    outcome,
    agent: invocation ? invocation.agentName : (details.agent as string | undefined) ?? null,
    agent_version: invocation?.agentVersion ?? null,
    team: invocation?.team ?? null,
    user_id: invocation?.userId ?? null,
    session_id: invocation?.sessionId ?? null,
    trace_id: invocation?.traceId ?? null,
    details,
  };
  try {
    configure();
    const line = JSON.stringify(event) + "\n";
    if (filePath) appendFileSync(filePath, line, "utf-8");
    else process.stdout.write(line);
    for (const sink of [...sinks]) {
      try {
        sink(event);
      } catch (err) {
        logger.error("Audit sink failed", err);
      }
    }
    const span = trace.getActiveSpan();
    if (span && trace.isSpanContextValid(span.spanContext())) {
      const attributes: Record<string, AttributeValue> = {};
      for (const [k, v] of Object.entries(details)) {
        if (typeof v === "string" || typeof v === "number" || typeof v === "boolean") attributes[k] = v;
      }
      span.addEvent(`audit.${eventType}`, { outcome, ...attributes });
    }
  } catch (err) {
    logger.error(`Failed to record audit event ${eventType}`, err);
  }
  return event;
}
