/**
 * Token budgets per invocation (SDK-12): a soft limit that warns and a hard limit that stops the run.
 *
 *     new EnterpriseAgentApp(graph, { name: "a", tokenBudget: { soft: 20_000, hard: 50_000 } });
 *     // or in agent.yaml:   token_budget: {soft: 20000, hard: 50000}
 *
 * Tokens are counted from the usage the model reports on each call (input + output). At the soft limit an audit
 * event is recorded once; at the hard limit the *next* model call is refused with `BudgetExceeded` and the caller
 * gets HTTP 429 `BudgetExceeded`. One very large call can overshoot the hard limit (it is checked between calls).
 * Calls that report no usage are not counted. Per-team/day budgets across agents belong to the gateway.
 */

import { BaseCallbackHandler } from "@langchain/core/callbacks/base";
import * as audit from "./audit.js";

/** The invocation used up its hard token budget. */
export class BudgetExceeded extends Error {
  readonly used: number;
  readonly hard: number;
  constructor(used: number, hard: number) {
    super(`Token budget exceeded (${used} of ${hard} tokens used).`);
    this.name = "BudgetExceeded";
    this.used = used;
    this.hard = hard;
  }
}

export interface TokenBudget {
  soft?: number;
  hard?: number;
}

/** Validate and normalise a budget (same rules as the manifest). */
export function tokenBudget(budget: TokenBudget): TokenBudget {
  for (const name of ["soft", "hard"] as const) {
    const v = budget[name];
    if (v !== undefined && !(Number.isInteger(v) && v > 0)) throw new Error(`token budget '${name}' must be positive.`);
  }
  if (budget.soft !== undefined && budget.hard !== undefined && budget.soft > budget.hard) {
    throw new Error("token budget 'soft' must not exceed 'hard'.");
  }
  return { ...budget };
}

/** Counts tokens for one invocation. */
export class BudgetTracker {
  inputTokens = 0;
  outputTokens = 0;
  private softReported = false;

  constructor(readonly budget: TokenBudget) {}

  get total(): number {
    return this.inputTokens + this.outputTokens;
  }

  add(inputTokens: number, outputTokens: number): void {
    this.inputTokens += inputTokens;
    this.outputTokens += outputTokens;
    const { soft, hard } = this.budget;
    if (soft && this.total >= soft && !this.softReported) {
      this.softReported = true;
      audit.emit(audit.BUDGET_SOFT, { outcome: "warned", tokens_used: this.total, soft_limit: soft, hard_limit: hard ?? null });
    }
  }

  /** Throw `BudgetExceeded` if the hard limit has been reached. */
  check(): void {
    const hard = this.budget.hard;
    if (hard !== undefined && this.total >= hard) {
      audit.emit(audit.BUDGET_EXCEEDED, { outcome: "blocked", tokens_used: this.total, hard_limit: hard });
      throw new BudgetExceeded(this.total, hard);
    }
  }
}

/** Counts model usage and refuses a model call once the hard budget is spent (the error type is preserved). */
export class BudgetGuard extends BaseCallbackHandler {
  name = "ent_budget_guard";
  override awaitHandlers = true;
  override raiseError = true;

  constructor(readonly tracker: BudgetTracker) {
    super();
  }

  override handleChatModelStart(): void {
    this.tracker.check();
  }

  override handleLLMStart(): void {
    this.tracker.check();
  }

  override handleLLMEnd(output: any): void {
    let input = 0;
    let out = 0;
    for (const generations of output?.generations ?? []) {
      for (const generation of generations) {
        const usage = generation?.message?.usage_metadata ?? {};
        input += usage.input_tokens ?? 0;
        out += usage.output_tokens ?? 0;
      }
    }
    const tokenUsage = output?.llmOutput?.tokenUsage;
    if (!input && !out && tokenUsage) {
      input = tokenUsage.promptTokens ?? 0;
      out = tokenUsage.completionTokens ?? 0;
    }
    if (input || out) this.tracker.add(input, out);
  }
}

/** Walk an error's `cause` chain looking for a `BudgetExceeded`. */
export function findBudgetExceeded(err: unknown): BudgetExceeded | undefined {
  for (let e: any = err, i = 0; e && i < 10; e = e.cause, i++) if (e instanceof BudgetExceeded) return e;
  return undefined;
}
