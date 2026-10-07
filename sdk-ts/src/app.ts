/**
 * EnterpriseAgentApp: serves a LangGraph graph on the Bedrock AgentCore runtime contract (SDK-01).
 *
 * Contract: GET /ping for health, POST /invocations for requests, port 8080. Every invocation is traced
 * (observability.ts), checked by guardrails on the way in and out (guardrails.ts) and carries the caller's identity
 * for audit events (audit.ts). Wire formats are identical to the Python SDK's `app.py`.
 */

import { createServer, type IncomingMessage, type Server, type ServerResponse } from "node:http";
import { HumanMessage, isBaseMessage, type BaseMessage } from "@langchain/core/messages";
import {
  ROOT_CONTEXT,
  SpanKind,
  context as otelContext,
  trace,
  type Attributes,
  type Context,
} from "@opentelemetry/api";
import type { BasicTracerProvider } from "@opentelemetry/sdk-trace-base";
import * as audit from "./audit.js";
import { BudgetGuard, BudgetTracker, findBudgetExceeded, tokenBudget as checkBudget, type TokenBudget } from "./budget.js";
import * as context from "./context.js";
import { Guardrails, GuardrailViolation } from "./guardrails.js";
import { getLogger, configureLogging } from "./log.js";
import * as manifestMod from "./manifest.js";
import * as metadata from "./metadata.js";
import * as obs from "./observability.js";

const logger = getLogger("ent_agent_sdk");

export const AGENTCORE_PORT = 8080;
export const SESSION_HEADER = "x-amzn-bedrock-agentcore-runtime-session-id";
export const USER_HEADER = "x-amzn-bedrock-agentcore-runtime-user-id";
export const TRACE_ID_HEADER = "x-ent-trace-id";

type Json = Record<string, unknown>;
export type InputMapper = (payload: Json) => unknown;
export type OutputMapper = (state: any) => Json;
export type HealthCheck = () => boolean | Promise<boolean>;

/** Standard request: {"prompt": "..."} becomes a MessagesState input. Anything else passes through. */
export function defaultInputMapper(payload: Json): Json {
  if (typeof payload.prompt === "string" && !("messages" in payload)) {
    const { prompt, ...rest } = payload;
    return { messages: [new HumanMessage(prompt)], ...rest };
  }
  return payload;
}

/** Standard response: {"output": <text of the last message>}; non-message states are returned as JSON. */
export function defaultOutputMapper(state: any): Json {
  if (state && typeof state === "object" && Array.isArray(state.messages) && state.messages.length) {
    const last = state.messages[state.messages.length - 1];
    if (isBaseMessage(last)) {
      const content = last.content;
      return { output: typeof content === "string" ? content : toJsonable(content) };
    }
  }
  const j = toJsonable(state);
  return j !== null && typeof j === "object" && !Array.isArray(j) ? (j as Json) : { output: j };
}

/** Convert graph state (which may hold LangChain messages) into JSON-safe data. */
export function toJsonable(value: unknown): unknown {
  if (isBaseMessage(value)) {
    const m = value as BaseMessage;
    return { type: m.getType(), content: toJsonable(m.content) };
  }
  if (Array.isArray(value)) return value.map(toJsonable);
  if (value === null || value === undefined) return null;
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") return value;
  if (typeof value === "object" && Object.getPrototypeOf(value) === Object.prototype) {
    return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, toJsonable(v)]));
  }
  return String(value);
}

export interface EnterpriseAgentAppOptions {
  name: string;
  version?: string;
  team?: string | null;
  dataClassification?: string | null;
  guardrails?: Guardrails;
  /** Tokens per invocation: soft warns (audit), hard refuses the next model call (HTTP 429). */
  tokenBudget?: TokenBudget;
  inputMapper?: InputMapper;
  outputMapper?: OutputMapper;
  /** Called by /ping; returning false or throwing reports Unhealthy (HTTP 503). */
  healthCheck?: HealthCheck;
  /** How long in-flight requests may finish on shutdown (SDK-16). Default 30000. */
  shutdownTimeoutMs?: number;
  /** For tests and special cases; by default built from OTEL_* settings. */
  tracerProvider?: BasicTracerProvider;
}

const TRACEPARENT = /^([0-9a-f]{2})-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$/;

/** Continue the caller's trace if it sent a W3C `traceparent` header. */
export function extractTraceparent(header: string | undefined): Context {
  const m = header ? TRACEPARENT.exec(header.trim().toLowerCase()) : null;
  if (!m || m[1] === "ff" || /^0+$/.test(m[2]) || /^0+$/.test(m[3])) return ROOT_CONTEXT;
  return trace.setSpanContext(ROOT_CONTEXT, {
    traceId: m[2],
    spanId: m[3],
    traceFlags: parseInt(m[4], 16) & 1,
    isRemote: true,
  });
}

function header(req: IncomingMessage, name: string): string | undefined {
  const v = req.headers[name];
  return Array.isArray(v) ? v[0] : v;
}

function send(res: ServerResponse, status: number, body: unknown, headers: Record<string, string> = {}): void {
  const text = JSON.stringify(body);
  res.writeHead(status, {
    "content-type": "application/json",
    "content-length": Buffer.byteLength(text),
    ...headers,
  });
  res.end(text);
}

function sendError(res: ServerResponse, status: number, type: string, message: string, traceId?: string): void {
  send(res, status, { error: { type, message } }, traceId ? { [TRACE_ID_HEADER]: traceId } : {});
}

async function readBody(req: IncomingMessage): Promise<Buffer> {
  const chunks: Buffer[] = [];
  for await (const chunk of req) chunks.push(chunk as Buffer);
  return Buffer.concat(chunks);
}

/** Wraps a compiled LangGraph graph and serves it on the AgentCore runtime contract. */
export class EnterpriseAgentApp {
  readonly graph: any;
  readonly name: string;
  readonly version: string;
  readonly team: string | null;
  readonly dataClassification: string | null;
  readonly guardrails: Guardrails;
  readonly tokenBudget: TokenBudget | null;
  readonly inputMapper: InputMapper;
  readonly outputMapper: OutputMapper;
  readonly healthCheck?: HealthCheck;
  readonly shutdownTimeoutMs: number;
  readonly tracerProvider: BasicTracerProvider;
  readonly tracer: ReturnType<BasicTracerProvider["getTracer"]>;
  private inFlight = 0;
  private lastStatusChange = Math.floor(Date.now() / 1000);
  private server?: Server;
  private shuttingDown?: Promise<void>;

  constructor(graph: any, options: EnterpriseAgentAppOptions) {
    this.graph = graph;
    this.name = options.name;
    this.version = options.version ?? "0.0.0";
    this.team = options.team ?? null;
    this.dataClassification = options.dataClassification ?? null;
    this.guardrails = options.guardrails ?? new Guardrails();
    this.tokenBudget = options.tokenBudget ? checkBudget(options.tokenBudget) : null;
    this.inputMapper = options.inputMapper ?? defaultInputMapper;
    this.outputMapper = options.outputMapper ?? defaultOutputMapper;
    this.healthCheck = options.healthCheck;
    this.shutdownTimeoutMs = options.shutdownTimeoutMs ?? 30_000;
    this.tracerProvider =
      options.tracerProvider ??
      obs.buildTracerProvider({
        serviceName: this.name,
        serviceVersion: this.version,
        team: this.team,
        extraAttributes: metadata.resourceAttributes(),
      });
    obs.ensureContextManager();
    this.tracer = this.tracerProvider.getTracer(obs.TRACER_NAME, metadata.sdkVersion());
    if (this.guardrails.disabled) {
      audit.emit(audit.WAIVER_IN_EFFECT, {
        outcome: "waived",
        agent: this.name,
        control: "guardrails",
        waiver: this.guardrails.waiver,
      });
    }
  }

  /** Create the app with name, team, data classification and guardrail profile from `agent.yaml`. */
  static fromManifest(
    graph: any,
    manifest: manifestMod.AgentManifest | string = "agent.yaml",
    options: Omit<Partial<EnterpriseAgentAppOptions>, "name"> = {},
  ): EnterpriseAgentApp {
    const loaded = typeof manifest === "string" ? manifestMod.load(manifest) : manifest;
    return new EnterpriseAgentApp(graph, {
      guardrails: new Guardrails(loaded.guardrailProfile),
      dataClassification: loaded.dataClassification,
      ...(Object.keys(loaded.tokenBudget).length ? { tokenBudget: loaded.tokenBudget } : {}),
      ...options,
      name: loaded.name,
      team: loaded.team,
    });
  }

  private setInFlight(delta: number): void {
    const wasBusy = this.inFlight > 0;
    this.inFlight += delta;
    if (wasBusy !== this.inFlight > 0) this.lastStatusChange = Math.floor(Date.now() / 1000);
  }

  private async healthy(): Promise<boolean> {
    if (!this.healthCheck) return true;
    try {
      return Boolean(await this.healthCheck());
    } catch (err) {
      logger.error(`Health check failed for agent ${this.name}`, err);
      return false;
    }
  }

  /** The request listener (use with `http.createServer`, or call `listen`). */
  readonly handler = (req: IncomingMessage, res: ServerResponse): void => {
    this.route(req, res).catch((err) => {
      logger.error(`Unhandled error in agent ${this.name}`, err);
      if (!res.headersSent) sendError(res, 500, "AgentError", "Error while running the agent.");
      else res.end();
    });
  };

  private async route(req: IncomingMessage, res: ServerResponse): Promise<void> {
    const path = (req.url ?? "/").split("?")[0];
    const method = req.method ?? "GET";
    if (path === "/ping") {
      if (method !== "GET") return this.notAllowed(res, "GET");
      if (!(await this.healthy())) {
        return send(res, 503, { status: "Unhealthy", time_of_last_update: Math.floor(Date.now() / 1000) });
      }
      return send(res, 200, {
        status: this.inFlight ? "HealthyBusy" : "Healthy",
        time_of_last_update: this.lastStatusChange,
      });
    }
    if (path === "/invocations") {
      if (method !== "POST") return this.notAllowed(res, "POST");
      return this.invoke(req, res);
    }
    send(res, 404, { detail: "Not Found" });
  }

  private notAllowed(res: ServerResponse, allow: string): void {
    send(res, 405, { detail: "Method Not Allowed" }, { allow });
  }

  private guardInput(payload: Json): Json {
    if (typeof payload.prompt === "string") return { ...payload, prompt: this.guardrails.enforce(payload.prompt, "input") };
    return payload;
  }

  private async invoke(req: IncomingMessage, res: ServerResponse): Promise<void> {
    let payload: unknown;
    try {
      const raw = await readBody(req);
      payload = JSON.parse(raw.length ? raw.toString("utf-8") : "{}");
    } catch {
      return sendError(res, 400, "InvalidRequest", "Request body must be valid JSON.");
    }
    if (payload === null || typeof payload !== "object" || Array.isArray(payload)) {
      return sendError(res, 400, "InvalidRequest", "Request body must be a JSON object.");
    }

    const sessionId = header(req, SESSION_HEADER);
    const userId = header(req, USER_HEADER);
    const attributes: Attributes = {
      [obs.OPERATION]: "invoke_agent",
      [obs.AGENT_NAME]: this.name,
      [obs.AGENT_VERSION]: this.version,
      [obs.SPAN_TYPE]: "agent",
    };
    if (this.team) attributes[obs.TEAM] = this.team;
    if (this.dataClassification) attributes[obs.DATA_CLASSIFICATION] = this.dataClassification;
    if (sessionId) attributes[obs.CONVERSATION_ID] = sessionId;

    const callerContext = extractTraceparent(header(req, "traceparent"));
    const span = this.tracer.startSpan(`invoke_agent ${this.name}`, { kind: SpanKind.SERVER, attributes }, callerContext);
    const spanContext = trace.setSpan(callerContext, span);
    const traceId = span.spanContext().traceId;
    const tracker = this.tokenBudget ? new BudgetTracker(this.tokenBudget) : undefined;
    const invocation: context.InvocationContext = {
      agentName: this.name,
      agentVersion: this.version,
      team: this.team,
      sessionId: sessionId ?? null,
      userId: userId ?? null,
      traceId,
      guardrails: this.guardrails,
      budget: tracker,
      dataClassification: this.dataClassification,
    };

    let body: Json | undefined;
    await otelContext.with(spanContext, () =>
      context.use(invocation, async () => {
        this.setInFlight(+1);
        try {
          try {
            payload = this.guardInput(payload as Json);
          } catch (err) {
            if (!(err instanceof GuardrailViolation)) throw err;
            span.setAttribute(obs.OUTCOME, "blocked");
            sendError(res, 400, "GuardrailBlocked", "Request blocked by guardrail policy.", traceId);
            return;
          }
          const config = {
            configurable: { thread_id: sessionId || "default" },
            metadata: { agent_name: this.name, agent_version: this.version, session_id: sessionId },
            callbacks: [
              new obs.GraphTracer(this.tracer, trace.setSpan(ROOT_CONTEXT, span)),
              ...(tracker ? [new BudgetGuard(tracker)] : []),
            ],
          };
          const state = await this.graph.invoke(this.inputMapper(payload as Json), config);
          let out = this.outputMapper(state);
          try {
            out = this.guardrails.enforcePayload(out, "output");
          } catch (err) {
            if (!(err instanceof GuardrailViolation)) throw err;
            span.setAttribute(obs.OUTCOME, "blocked");
            sendError(res, 502, "GuardrailBlocked", "Response withheld by guardrail policy.", traceId);
            return;
          }
          span.setAttribute(obs.OUTCOME, "success");
          body = out;
        } catch (err) {
          if (err instanceof GuardrailViolation) {
            // raised inside a tool and not handled by the graph
            span.setAttribute(obs.OUTCOME, "blocked");
            sendError(res, 502, "GuardrailBlocked", "Response withheld by guardrail policy.", traceId);
            return;
          }
          if (findBudgetExceeded(err)) {
            // already audited when raised
            span.setAttribute(obs.OUTCOME, "budget_exceeded");
            sendError(res, 429, "BudgetExceeded", "The token budget for this invocation is used up.", traceId);
            return;
          }
          // the contract must always answer; details go to logs, not callers
          obs.markError(span, err);
          span.setAttribute(obs.OUTCOME, "error");
          logger.error(`Invocation failed for agent ${this.name}`, err);
          sendError(res, 500, "AgentError", `${obs.errorType(err)} while running the agent.`, traceId);
        } finally {
          this.setInFlight(-1);
          if (tracker) {
            span.setAttribute(obs.INPUT_TOKENS, tracker.inputTokens);
            span.setAttribute(obs.OUTPUT_TOKENS, tracker.outputTokens);
          }
          span.end();
        }
      }),
    );
    if (body === undefined) return;
    send(res, 200, sessionId ? { ...body, session_id: sessionId } : body, { [TRACE_ID_HEADER]: traceId });
  }

  /** Start listening. `port` 0 picks a free port. Returns the HTTP server. */
  listen(port: number = Number(process.env.PORT ?? AGENTCORE_PORT), host = "0.0.0.0"): Promise<Server> {
    this.server = createServer(this.handler);
    return new Promise((resolve, reject) => {
      this.server!.once("error", reject);
      this.server!.listen(port, host, () => resolve(this.server!));
    });
  }

  /** Stop accepting requests, let in-flight ones finish (up to `shutdownTimeoutMs`), then flush spans. */
  shutdown(): Promise<void> {
    this.shuttingDown ??= (async () => {
      const server = this.server;
      const closed = server ? new Promise<void>((resolve) => server.close(() => resolve())) : Promise.resolve();
      server?.closeIdleConnections();
      const deadline = Date.now() + this.shutdownTimeoutMs;
      while (this.inFlight > 0 && Date.now() < deadline) await new Promise((r) => setTimeout(r, 25));
      if (this.inFlight > 0) logger.warning(`Shutdown timeout: ${this.inFlight} request(s) still running`);
      server?.closeAllConnections();
      await closed;
      try {
        await this.tracerProvider.shutdown(); // send any buffered spans
      } catch (err) {
        logger.error("Span flush failed during shutdown", err);
      }
    })();
    return this.shuttingDown;
  }

  /** Serve the agent with JSON logs. AgentCore requires port 8080; `PORT` overrides it for local runs. */
  async run(host = "0.0.0.0", port?: number): Promise<void> {
    const p = port ?? Number(process.env.PORT ?? AGENTCORE_PORT);
    configureLogging(this.name);
    await this.listen(p, host);
    logger.info(`Starting ${this.name} ${this.version} on ${host}:${p}`);
    const stop = (): void => {
      logger.info("Shutting down");
      void this.shutdown().then(() => process.exit(0));
    };
    process.once("SIGTERM", stop);
    process.once("SIGINT", stop);
  }
}
