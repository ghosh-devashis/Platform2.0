/**
 * Tool registry and allowlist (SDK-09), with guardrails, audit and resilience built in.
 *
 *     const lookupCustomer = defineTool({
 *       name: "lookup_customer", description: "Look up a customer by ID.",
 *       schema: z.object({ customerId: z.string() }),
 *       owner: "crm-team", dataClassification: "confidential", sideEffects: false,
 *       func: async ({ customerId }) => "...",
 *     });
 *     const llm = bind(getModel("chat-default"), [lookupCustomer]);   // fails if a tool isn't registered
 *
 * Every registered tool: carries metadata (owner, data classification, side effects) that appears on its trace span;
 * has its arguments and result checked by the guardrails of the running invocation (stages `tool_input` and
 * `tool_output`); has a timeout, and read-only tools get one bounded retry with backoff (SDK-16). Tools with side
 * effects are never retried automatically and every call is written to the audit stream (SDK-13). Failures are
 * reported as `ToolError` carrying the error type only; details go to the log.
 */

import { tool as lcTool, type DynamicStructuredTool } from "@langchain/core/tools";
import type { z } from "zod";
import * as audit from "./audit.js";
import * as context from "./context.js";
import { Guardrails, GuardrailViolation } from "./guardrails.js";
import { getLogger } from "./log.js";

const logger = getLogger("ent_agent_sdk.tools");

export const DATA_CLASSIFICATIONS = ["public", "internal", "confidential", "restricted"] as const;
export const DEFAULT_TIMEOUT_MS = 30_000;
export const DEFAULT_READ_RETRIES = 1;

/** A tool failed. The message names the tool and the error type, never the underlying message. */
export class ToolError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ToolError";
  }
}

/** A tool did not finish within its timeout. */
export class ToolTimeoutError extends ToolError {
  constructor(message: string) {
    super(message);
    this.name = "ToolTimeoutError";
  }
}

/** A tool that is not in the registry was bound or used. */
export class UnregisteredToolError extends ToolError {
  constructor(message: string) {
    super(message);
    this.name = "UnregisteredToolError";
  }
}

export interface ToolMetadata {
  name: string;
  owner: string;
  dataClassification: string;
  sideEffects: boolean;
  timeoutMs: number;
  retries: number;
}

export interface DefineToolOptions<S extends z.ZodObject<any>> {
  name: string;
  description: string;
  schema: S;
  func: (args: z.infer<S>, options: { signal: AbortSignal }) => unknown | Promise<unknown>;
  owner: string;
  dataClassification?: string;
  sideEffects?: boolean;
  timeoutMs?: number;
  /** Applies to read-only tools only. */
  retries?: number;
}

const backoffMs = (attempt: number): number => Math.min(0.2 * 2 ** attempt, 2.0) * 1000;
const sleep = (ms: number): Promise<void> => new Promise((resolve) => setTimeout(resolve, ms));

function activeGuardrails(): Guardrails {
  const invocation = context.current();
  return invocation?.guardrails ?? new Guardrails();
}

function auditCall(meta: ToolMetadata, outcome: string, extra: Record<string, unknown> = {}): void {
  if (meta.sideEffects) {
    audit.emit(audit.TOOL_CALL, {
      outcome,
      tool: meta.name,
      owner: meta.owner,
      data_classification: meta.dataClassification,
      side_effects: true,
      ...extra,
    });
  }
}

function fail(meta: ToolMetadata, err: unknown): ToolError {
  const type = err instanceof Error ? err.constructor.name : "Error";
  logger.error(`Tool ${meta.name} failed: ${type}`, err);
  auditCall(meta, "failure", { error_type: type });
  if (err instanceof ToolError) return err;
  return new ToolError(`Tool '${meta.name}' failed (${type}).`);
}

async function withTimeout<T>(meta: ToolMetadata, run: (signal: AbortSignal) => Promise<T>): Promise<T> {
  const controller = new AbortController();
  let timer: NodeJS.Timeout | undefined;
  const timeout = new Promise<never>((_, reject) => {
    timer = setTimeout(() => {
      controller.abort();
      reject(new ToolTimeoutError(`Tool '${meta.name}' timed out after ${meta.timeoutMs / 1000}s.`));
    }, meta.timeoutMs);
  });
  try {
    return await Promise.race([run(controller.signal), timeout]);
  } finally {
    clearTimeout(timer);
  }
}

async function callTool(meta: ToolMetadata, func: DefineToolOptions<any>["func"], args: Record<string, unknown>): Promise<unknown> {
  const guardrails = activeGuardrails();
  const safe = guardrails.enforcePayload(args, "tool_input");
  auditCall(meta, "started");
  let error: unknown;
  for (let attempt = 0; attempt <= meta.retries; attempt++) {
    try {
      const result = await withTimeout(meta, async (signal) => func(safe, { signal }));
      const checked = guardrails.enforcePayload(result, "tool_output");
      auditCall(meta, "success");
      return checked;
    } catch (err) {
      if (err instanceof GuardrailViolation) throw err;
      error = err;
    }
    if (attempt < meta.retries) await sleep(backoffMs(attempt));
  }
  throw fail(meta, error);
}

/** The approved tools of an agent. Only registered tools can be bound to a model. */
export class ToolRegistry {
  private readonly tools = new Map<string, DynamicStructuredTool>();
  private readonly meta = new Map<string, ToolMetadata>();

  /** Register a function as a tool. */
  defineTool<S extends z.ZodObject<any>>(options: DefineToolOptions<S>): DynamicStructuredTool {
    const owner = options.owner?.trim();
    if (!owner) throw new Error("A tool needs an owner (team or person responsible for it).");
    const dataClassification = options.dataClassification ?? "internal";
    if (!(DATA_CLASSIFICATIONS as readonly string[]).includes(dataClassification)) {
      throw new Error(`dataClassification must be one of ${JSON.stringify(DATA_CLASSIFICATIONS)}.`);
    }
    const sideEffects = options.sideEffects ?? false;
    if (sideEffects && options.retries) {
      throw new Error("Tools with side effects are never retried automatically; remove `retries`.");
    }
    if (this.tools.has(options.name)) throw new Error(`A tool named '${options.name}' is already registered.`);

    const meta: ToolMetadata = {
      name: options.name,
      owner,
      dataClassification,
      sideEffects,
      timeoutMs: options.timeoutMs ?? DEFAULT_TIMEOUT_MS,
      retries: sideEffects ? 0 : (options.retries ?? DEFAULT_READ_RETRIES),
    };
    const registered = lcTool(
      async (args: Record<string, unknown>) => (await callTool(meta, options.func, args)) as any,
      {
        name: options.name,
        description: options.description,
        schema: options.schema,
        metadata: {
          ent_registered: true,
          ent_tool_owner: meta.owner,
          ent_tool_classification: meta.dataClassification,
          ent_tool_side_effects: meta.sideEffects,
        },
      } as any,
    ) as unknown as DynamicStructuredTool;
    this.tools.set(options.name, registered);
    this.meta.set(options.name, meta);
    return registered;
  }

  get(name: string): DynamicStructuredTool {
    const found = this.tools.get(name);
    if (!found) throw new UnregisteredToolError(`Tool '${name}' is not registered.`);
    return found;
  }

  metadata(name: string): ToolMetadata {
    this.get(name);
    return this.meta.get(name)!;
  }

  names(): string[] {
    return [...this.tools.keys()].sort();
  }

  all(): DynamicStructuredTool[] {
    return this.names().map((n) => this.tools.get(n)!);
  }

  /** Return `tools` as a list, or throw `UnregisteredToolError` if any is not in this registry. */
  requireRegistered<T extends { name: string }>(tools: Iterable<T>): T[] {
    const checked: T[] = [];
    for (const candidate of tools) {
      if ((this.tools.get(candidate?.name) as unknown) !== candidate) {
        throw new UnregisteredToolError(
          `Tool '${candidate?.name ?? String(candidate)}' is not registered. Declare it with defineTool({owner, dataClassification, ...}).`,
        );
      }
      checked.push(candidate);
    }
    return checked;
  }

  /** `model.bindTools(...)` for registered tools only (all registered tools if `tools` is omitted). */
  bind<M extends { bindTools: (tools: any[], ...rest: any[]) => any }>(model: M, tools?: Iterable<{ name: string }>): ReturnType<M["bindTools"]> {
    return model.bindTools(this.requireRegistered(tools ?? this.all()));
  }
}

export const defaultRegistry = new ToolRegistry();
export const defineTool = defaultRegistry.defineTool.bind(defaultRegistry);
export const bind = defaultRegistry.bind.bind(defaultRegistry);
export const get = defaultRegistry.get.bind(defaultRegistry);
export const requireRegistered = defaultRegistry.requireRegistered.bind(defaultRegistry);
