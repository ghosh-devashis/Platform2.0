/**
 * Model access (SDK-07, GW-01, GW-04): approved models by logical name, always through the LLM gateway.
 *
 *     const llm = getModel("chat-default");   // a LangChain chat model: invoke, bindTools, streaming
 *
 * The model talks to the gateway's OpenAI-compatible API, never to a provider directly. Every request carries
 * correlation headers so gateway logs join the agent's trace (GW-04): `x-portkey-trace-id`, `x-portkey-metadata`
 * (agent, version, team, environment) and W3C `traceparent`.
 *
 * Resilience (SDK-16): the SDK does not retry LLM calls (`maxRetries: 0`); retries and fallbacks are the gateway's job.
 *
 * Configuration: `ENT_MODEL_GATEWAY_URL` (default http://localhost:8788/v1) and `ENT_MODEL_GATEWAY_KEY`.
 */

import { trace } from "@opentelemetry/api";
import { ChatOpenAI, type ChatOpenAIFields } from "@langchain/openai";
import * as context from "./context.js";
import * as obs from "./observability.js";

export const GATEWAY_URL_ENV = "ENT_MODEL_GATEWAY_URL";
export const GATEWAY_KEY_ENV = "ENT_MODEL_GATEWAY_KEY";
export const DEFAULT_GATEWAY_URL = "http://localhost:8788/v1";
export const DEFAULT_TIMEOUT_MS = 120_000;

/** Settings agents may not override: they would bypass the gateway or its credentials (GW-01). */
const LOCKED = [
  "baseURL", "configuration", "apiKey", "openAIApiKey", "maxRetries", "fetch", "fetchOptions", "httpAgent",
  "defaultHeaders", "openAIProxy",
];

/** getModel() was called with settings that would bypass the gateway. */
export class ModelConfigError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ModelConfigError";
  }
}

/** Return a chat model for an approved logical model name (e.g. "chat-default"), routed via the gateway. */
export function getModel(
  name: string,
  options: { timeoutMs?: number } & Omit<ChatOpenAIFields, "model" | "modelName" | "timeout" | "configuration" | "apiKey" | "openAIApiKey" | "maxRetries"> = {},
): ChatOpenAI {
  if (!name) throw new ModelConfigError("Model name must not be empty.");
  const { timeoutMs = DEFAULT_TIMEOUT_MS, ...rest } = options as Record<string, unknown> & { timeoutMs?: number };
  const locked = LOCKED.filter((k) => k in rest).sort();
  if (locked.length) {
    throw new ModelConfigError(
      `getModel() does not allow overriding ${JSON.stringify(locked)}; all traffic goes through the gateway.`,
    );
  }
  return new ChatOpenAI({
    ...(rest as ChatOpenAIFields),
    model: name,
    apiKey: process.env[GATEWAY_KEY_ENV] ?? "local-dev",
    timeout: timeoutMs,
    maxRetries: 0,
    configuration: {
      baseURL: process.env[GATEWAY_URL_ENV] ?? DEFAULT_GATEWAY_URL,
      // Looked up per request so the headers follow the active trace, and so tests can stub global fetch.
      fetch: (url: any, init: any) => {
        const headers = new Headers(init?.headers);
        for (const [k, v] of Object.entries(correlationHeaders())) headers.set(k, v);
        return globalThis.fetch(url, { ...init, headers });
      },
    },
  });
}

/**
 * Headers linking a gateway request to the current agent trace, plus the agent's guardrail profile and data
 * classification (sent even outside a trace, whenever an invocation is running).
 */
export function correlationHeaders(): Record<string, string> {
  const headers: Record<string, string> = {};
  const invocation = context.current();
  if (invocation) {
    if (invocation.guardrails && invocation.guardrails.enabled) headers["x-ent-guardrail-profile"] = invocation.guardrails.profile;
    if (invocation.dataClassification) headers["x-ent-data-classification"] = invocation.dataClassification;
  }
  const span = trace.getActiveSpan();
  if (!span) return headers;
  const sc = span.spanContext();
  if (!trace.isSpanContextValid(sc)) return headers;
  headers["x-portkey-trace-id"] = sc.traceId;
  const attributes: Record<string, unknown> = (span as any).attributes ?? {};
  const metadata: Record<string, string> = {};
  for (const [key, attribute] of [["agent", obs.AGENT_NAME], ["agent_version", obs.AGENT_VERSION], ["team", obs.TEAM]] as const) {
    if (attribute in attributes) metadata[key] = String(attributes[attribute]);
  }
  const environment = (span as any).resource?.attributes?.["deployment.environment"];
  if (environment) metadata.environment = String(environment);
  if (Object.keys(metadata).length) headers["x-portkey-metadata"] = JSON.stringify(metadata);
  const flags = (sc.traceFlags & 0xff).toString(16).padStart(2, "0");
  headers.traceparent = `00-${sc.traceId}-${sc.spanId}-${flags}`;
  if (sc.traceState) headers.tracestate = sc.traceState.serialize();
  return headers;
}
