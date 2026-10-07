/**
 * Observability (SDK-03, SDK-04): OpenTelemetry traces and JSON logs for every agent, with no developer code.
 *
 *     invoke_agent <agent>          (server span: agent name/version/team, session, outcome)
 *       `- <graph node>             (one span per LangGraph node and nested chain)
 *            |- chat <model>        (LLM calls: model, provider, token usage)
 *            `- execute_tool <name> (tool calls)
 *
 * Attribute names follow the OpenTelemetry GenAI semantic conventions, plus `fiddler.span.type`.
 * Prompt/response content and error messages are never recorded (only the error type).
 *
 * Where traces go is configuration only: `OTEL_EXPORTER_OTLP_ENDPOINT` (unset = no export),
 * `OTEL_EXPORTER_OTLP_HEADERS`, `OTEL_RESOURCE_ATTRIBUTES`, `OTEL_SDK_DISABLED=true` (off).
 * Telemetry fails open (NFR-03): instrumentation errors are logged and never fail an invocation.
 */

import { AsyncLocalStorage } from "node:async_hooks";
import {
  ROOT_CONTEXT,
  SpanStatusCode,
  context as otelContext,
  trace,
  type Attributes,
  type Context,
  type ContextManager,
  type Span,
  type Tracer,
} from "@opentelemetry/api";
import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-http";
import { defaultResource, detectResources, envDetector, resourceFromAttributes } from "@opentelemetry/resources";
import { BasicTracerProvider, BatchSpanProcessor, type SpanProcessor } from "@opentelemetry/sdk-trace-base";
import { BaseCallbackHandler } from "@langchain/core/callbacks/base";
import { isGraphBubbleUp } from "@langchain/langgraph";
import { getLogger } from "./log.js";
import { sdkVersion } from "./metadata.js";

const logger = getLogger("ent_agent_sdk.observability");

export const TRACER_NAME = "ent_agent_sdk";
export const HIDDEN_TAG = "langsmith:hidden"; // LangGraph tags its internal plumbing runs with this

// Attribute names (OpenTelemetry GenAI semantic conventions, plus ent.* for enterprise fields).
export const OPERATION = "gen_ai.operation.name";
export const AGENT_NAME = "gen_ai.agent.name";
export const CONVERSATION_ID = "gen_ai.conversation.id";
export const REQUEST_MODEL = "gen_ai.request.model";
export const RESPONSE_MODEL = "gen_ai.response.model";
export const PROVIDER = "gen_ai.provider.name";
export const INPUT_TOKENS = "gen_ai.usage.input_tokens";
export const OUTPUT_TOKENS = "gen_ai.usage.output_tokens";
export const TOOL_NAME = "gen_ai.tool.name";
export const ERROR_TYPE = "error.type";
export const AGENT_VERSION = "ent.agent.version";
export const TEAM = "ent.team";
export const OUTCOME = "ent.outcome";
export const NODE = "ent.graph.node";
export const SPAN_TYPE = "fiddler.span.type";
export const DATA_CLASSIFICATION = "ent.data_classification";
export const TOOL_OWNER = "ent.tool.owner";
export const TOOL_CLASSIFICATION = "ent.tool.data_classification";
export const TOOL_SIDE_EFFECTS = "ent.tool.side_effects";

export const sdkVersionString = sdkVersion;

/**
 * OpenTelemetry's context manager for Node, built on AsyncLocalStorage (the standard one lives in a package that is
 * not on the approved list). Makes "the active span" follow async calls.
 */
class AlsContextManager implements ContextManager {
  private readonly als = new AsyncLocalStorage<Context>();
  active(): Context {
    return this.als.getStore() ?? ROOT_CONTEXT;
  }
  with<A extends unknown[], F extends (...args: A) => ReturnType<F>>(
    ctx: Context,
    fn: F,
    thisArg?: ThisParameterType<F>,
    ...args: A
  ): ReturnType<F> {
    return this.als.run(ctx, () => fn.call(thisArg as ThisParameterType<F>, ...args));
  }
  bind<T>(ctx: Context, target: T): T {
    if (typeof target !== "function") return target;
    const manager = this;
    return function (this: unknown, ...args: unknown[]) {
      return manager.with(ctx, () => (target as (...a: unknown[]) => unknown).apply(this, args));
    } as unknown as T;
  }
  enable(): this {
    return this;
  }
  disable(): this {
    this.als.disable();
    return this;
  }
}

let contextManagerInstalled = false;
export function ensureContextManager(): void {
  if (contextManagerInstalled) return;
  contextManagerInstalled = true;
  otelContext.setGlobalContextManager(new AlsContextManager());
}

export interface TracerProviderOptions {
  serviceName: string;
  serviceVersion: string;
  team?: string | null;
  extraAttributes?: Record<string, string>;
  /** Extra span processors (tests use an in-memory exporter). */
  spanProcessors?: SpanProcessor[];
}

/** Create the agent's tracer provider; the exporter is chosen from standard OTEL_* settings. */
export function buildTracerProvider(options: TracerProviderOptions): BasicTracerProvider {
  ensureContextManager();
  const attributes: Record<string, string> = {
    "service.name": options.serviceName,
    "service.version": options.serviceVersion,
    "ent.sdk.version": sdkVersion(),
  };
  if (options.team) attributes[TEAM] = options.team;
  Object.assign(attributes, options.extraAttributes ?? {});
  // Explicit attributes win over OTEL_RESOURCE_ATTRIBUTES, which win over defaults.
  const resource = defaultResource()
    .merge(detectResources({ detectors: [envDetector] }))
    .merge(resourceFromAttributes(attributes));

  const processors: SpanProcessor[] = [...(options.spanProcessors ?? [])];
  if (process.env.OTEL_SDK_DISABLED?.trim().toLowerCase() === "true") {
    logger.info("Tracing disabled (OTEL_SDK_DISABLED=true)");
  } else if (process.env.OTEL_EXPORTER_OTLP_TRACES_ENDPOINT || process.env.OTEL_EXPORTER_OTLP_ENDPOINT) {
    processors.push(new BatchSpanProcessor(new OTLPTraceExporter()));
  } else {
    logger.info("No OTEL_EXPORTER_OTLP_ENDPOINT set; traces are not exported");
  }
  const provider = new BasicTracerProvider({ resource, spanProcessors: processors });
  // Make it the global provider too (first one wins), so spans agents create themselves land in the same trace.
  trace.setGlobalTracerProvider(provider);
  return provider;
}

/** Record a failure on a span: the error type only, never the message (it may contain data). */
export function markError(span: Span, error: unknown): void {
  const type = errorType(error);
  span.setAttribute(ERROR_TYPE, type);
  span.setStatus({ code: SpanStatusCode.ERROR, message: type });
}

export function errorType(error: unknown): string {
  return error instanceof Error ? error.constructor.name || error.name : "Error";
}

function serializedName(serialized: any): string | undefined {
  if (serialized && typeof serialized === "object") {
    return serialized.name ?? (Array.isArray(serialized.id) ? serialized.id[serialized.id.length - 1] : undefined);
  }
  return undefined;
}

/**
 * LangChain callback handler that turns graph, node, LLM and tool runs into OpenTelemetry spans.
 * One instance per invocation. `parent` is the context holding the invocation's server span.
 */
export class GraphTracer extends BaseCallbackHandler {
  name = "ent_graph_tracer";
  override awaitHandlers = true; // run in order, inside the graph's own async context
  override raiseError = false;
  private readonly spans = new Map<string, Span>();
  private readonly contexts = new Map<string, Context>();

  constructor(
    private readonly tracer: Tracer,
    private readonly parent: Context,
  ) {
    super();
  }

  // Chains: the graph itself, its nodes, and runnables inside nodes.
  override handleChainStart(chain: any, _inputs: any, runId: string, parentRunId?: string, tags?: string[], metadata?: Record<string, unknown>, _runType?: string, runName?: string): void {
    this.guard(() => {
      if (!parentRunId || (tags ?? []).includes(HIDDEN_TAG)) {
        // Top-level graph run (the server span covers it) or internal plumbing: no span of its own.
        this.contexts.set(runId, this.parentContext(parentRunId));
        return;
      }
      const attributes: Attributes = { [SPAN_TYPE]: "chain" };
      const node = metadata?.langgraph_node;
      if (typeof node === "string" && node) attributes[NODE] = node;
      this.start(runId, parentRunId, runName ?? serializedName(chain) ?? "chain", attributes);
    });
  }
  override handleChainEnd(_outputs: any, runId: string): void {
    this.guard(() => this.end(runId));
  }
  override handleChainError(err: any, runId: string): void {
    this.guard(() => this.end(runId, err));
  }

  // LLM calls.
  override handleChatModelStart(llm: any, _messages: any, runId: string, parentRunId?: string, _extra?: Record<string, unknown>, _tags?: string[], metadata?: Record<string, unknown>, runName?: string): void {
    this.guard(() => this.startLlm(llm, runId, parentRunId, metadata, runName));
  }
  override handleLLMStart(llm: any, _prompts: string[], runId: string, parentRunId?: string, _extra?: Record<string, unknown>, _tags?: string[], metadata?: Record<string, unknown>, runName?: string): void {
    this.guard(() => this.startLlm(llm, runId, parentRunId, metadata, runName));
  }
  override handleLLMEnd(output: any, runId: string): void {
    this.guard(() => {
      let input = 0;
      let out = 0;
      let responseModel: string | undefined;
      for (const generations of output?.generations ?? []) {
        for (const generation of generations) {
          const message = generation?.message;
          const usage = message?.usage_metadata ?? {};
          input += usage.input_tokens ?? 0;
          out += usage.output_tokens ?? 0;
          responseModel ??= message?.response_metadata?.model_name;
        }
      }
      const tokenUsage = output?.llmOutput?.tokenUsage;
      if (!input && !out && tokenUsage) {
        input = tokenUsage.promptTokens ?? 0;
        out = tokenUsage.completionTokens ?? 0;
      }
      const attributes: Attributes = {};
      if (input || out) {
        attributes[INPUT_TOKENS] = input;
        attributes[OUTPUT_TOKENS] = out;
      }
      if (responseModel) attributes[RESPONSE_MODEL] = responseModel;
      this.end(runId, undefined, attributes);
    });
  }
  override handleLLMError(err: any, runId: string): void {
    this.guard(() => this.end(runId, err));
  }

  // Tool calls.
  override handleToolStart(tool: any, _input: string, runId: string, parentRunId?: string, _tags?: string[], metadata?: Record<string, unknown>, runName?: string): void {
    this.guard(() => {
      const name = runName ?? serializedName(tool) ?? "tool";
      const attributes: Attributes = { [OPERATION]: "execute_tool", [TOOL_NAME]: name, [SPAN_TYPE]: "tool" };
      const meta = metadata ?? {};
      if (meta.ent_tool_owner) {
        // registered tools (tools.ts) describe themselves
        attributes[TOOL_OWNER] = String(meta.ent_tool_owner);
        attributes[TOOL_CLASSIFICATION] = String(meta.ent_tool_classification ?? "unknown");
        attributes[TOOL_SIDE_EFFECTS] = Boolean(meta.ent_tool_side_effects ?? false);
      }
      this.start(runId, parentRunId, `execute_tool ${name}`, attributes);
    });
  }
  override handleToolEnd(_output: any, runId: string): void {
    this.guard(() => this.end(runId));
  }
  override handleToolError(err: any, runId: string): void {
    this.guard(() => this.end(runId, err));
  }

  // Helpers.
  private guard(fn: () => void): void {
    try {
      fn();
    } catch (err) {
      logger.warning("Tracing callback failed (ignored)", err);
    }
  }
  private startLlm(llm: any, runId: string, parentRunId: string | undefined, metadata: Record<string, unknown> | undefined, runName?: string): void {
    const m = metadata ?? {};
    const model = (m.ls_model_name as string) || runName || serializedName(llm) || "unknown";
    const attributes: Attributes = { [OPERATION]: "chat", [REQUEST_MODEL]: model, [SPAN_TYPE]: "llm" };
    if (m.ls_provider) attributes[PROVIDER] = String(m.ls_provider);
    this.start(runId, parentRunId, `chat ${model}`, attributes);
  }
  private parentContext(parentRunId?: string): Context {
    return (parentRunId ? this.contexts.get(parentRunId) : undefined) ?? this.parent;
  }
  private start(runId: string, parentRunId: string | undefined, name: string, attributes: Attributes): void {
    const ctx = this.parentContext(parentRunId);
    const span = this.tracer.startSpan(name, { attributes }, ctx);
    this.spans.set(runId, span);
    this.contexts.set(runId, trace.setSpan(ctx, span));
  }
  private end(runId: string, error?: unknown, attributes?: Attributes): void {
    this.contexts.delete(runId);
    const span = this.spans.get(runId);
    this.spans.delete(runId);
    if (!span) return;
    if (attributes) span.setAttributes(attributes);
    if (error !== undefined && !(typeof isGraphBubbleUp === "function" && isGraphBubbleUp(error))) markError(span, error);
    span.end();
  }
}
