/**
 * Guardrails (SDK-08, layer 2): checks the gateway can't see.
 *
 * Stages: `input` (user request), `tool_input` (arguments the model chose), `tool_output` (what a tool returned;
 * the main route for prompt injection) and `output` (the final answer).
 *
 * Built-in detectors find personal data (`pii`), credentials (`secret`) and injection attempts (`injection`).
 * A profile decides, per category and stage, whether to allow, redact or block. `standard` suits most agents;
 * `strict` is required for `restricted` data (see manifest.ts).
 *
 * Fail closed. Disabling guardrails needs an approved waiver ID (`Guardrails.off({waiver})`) and emits an audit
 * event. Detection is pattern-based: a safety net, not a replacement for the gateway or Bedrock layers.
 */

import * as audit from "./audit.js";

export const STAGES = ["input", "tool_input", "tool_output", "output"] as const;
export type Stage = (typeof STAGES)[number];

export const Action = { ALLOW: "allow", REDACT: "redact", BLOCK: "block" } as const;
export type Action = (typeof Action)[keyof typeof Action];

/** A guardrail blocked the content. The message names the stage and categories, never the content. */
export class GuardrailViolation extends Error {
  readonly stage: string;
  readonly categories: string[];

  constructor(stage: string, categories: readonly string[]) {
    const cats = [...new Set(categories)].sort();
    super(`Blocked by guardrail policy at stage '${stage}' (${cats.join(", ")}).`);
    this.name = "GuardrailViolation";
    this.stage = stage;
    this.categories = cats;
  }
}

/** An external guardrail could not be reached; the request is blocked (fail closed). */
export class GuardrailUnavailable extends GuardrailViolation {
  constructor(stage: string) {
    super(stage, ["unavailable"]);
    this.name = "GuardrailUnavailable";
  }
}

export interface Match {
  start: number;
  end: number;
  category: string;
  rule: string;
}

// --- Detectors (same patterns as the Python SDK) -------------------------------------------------------------

const EMAIL = /\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b/g;
const SSN = /\b(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}\b/g;
const CARD = /\b(?:\d[ -]?){13,19}\b/g;
const PHONE = /(?<!\d)(?:\+?1[ .-]?)?\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}(?!\d)/g;

const SECRETS: Record<string, RegExp> = {
  "aws-access-key": /\b(?:AKIA|ASIA)[0-9A-Z]{16}\b/g,
  "api-key": /\bsk-[A-Za-z0-9_-]{20,}\b/g,
  "github-token": /\bgh[pousr]_[A-Za-z0-9]{30,}\b/g,
  "private-key": /-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----/g,
  jwt: /\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b/g,
};

const INJECTION: RegExp[] = [
  /ignore (?:all |any |the )?(?:previous|prior|above|earlier) (?:instructions|prompts|messages|rules)/gi,
  /disregard (?:all |any |the )?(?:previous |prior |above |earlier )?(?:instructions|rules|guidelines)/gi,
  /(?:forget|override) (?:all |your )?(?:previous |prior )?(?:instructions|rules)/gi,
  /(?:reveal|show|print|repeat) (?:me )?(?:your|the) (?:system|hidden|initial) (?:prompt|instructions)/gi,
  /you are now (?:in )?(?:developer mode|dan\b|jailbroken)/gi,
  /\bnew instructions\s*:/gi,
];

export function luhnOk(digits: string): boolean {
  let total = 0;
  const parity = digits.length % 2;
  for (let i = 0; i < digits.length; i++) {
    let d = Number(digits[i]);
    if (i % 2 === parity) d = d * 2 > 9 ? d * 2 - 9 : d * 2;
    total += d;
  }
  return total % 10 === 0;
}

/** All matches of the built-in detectors in `text`. */
export function detect(text: string): Match[] {
  const matches: Match[] = [];
  const add = (m: RegExpMatchArray, category: string, rule: string): void => {
    matches.push({ start: m.index!, end: m.index! + m[0].length, category, rule });
  };
  for (const m of text.matchAll(EMAIL)) add(m, "pii", "email");
  for (const m of text.matchAll(SSN)) add(m, "pii", "us-ssn");
  for (const m of text.matchAll(CARD)) {
    const digits = m[0].replace(/\D/g, "");
    if (digits.length >= 13 && digits.length <= 19 && luhnOk(digits)) add(m, "pii", "payment-card");
  }
  for (const m of text.matchAll(PHONE)) add(m, "pii", "phone");
  for (const [rule, pattern] of Object.entries(SECRETS)) for (const m of text.matchAll(pattern)) add(m, "secret", rule);
  for (const pattern of INJECTION) for (const m of text.matchAll(pattern)) add(m, "injection", "prompt-injection");
  return matches;
}

// --- Profiles ------------------------------------------------------------------------------------------------

type Row = Record<Stage, Action>;
const row = (input: Action, toolInput: Action, toolOutput: Action, output: Action): Row => ({
  input,
  tool_input: toolInput,
  tool_output: toolOutput,
  output,
});

const A = Action;
export const PROFILES: Record<string, Record<string, Row>> = {
  standard: {
    pii: row(A.ALLOW, A.ALLOW, A.REDACT, A.REDACT),
    secret: row(A.REDACT, A.BLOCK, A.REDACT, A.REDACT),
    injection: row(A.ALLOW, A.ALLOW, A.BLOCK, A.ALLOW),
  },
  strict: {
    pii: row(A.BLOCK, A.BLOCK, A.REDACT, A.BLOCK),
    secret: row(A.BLOCK, A.BLOCK, A.BLOCK, A.BLOCK),
    injection: row(A.BLOCK, A.BLOCK, A.BLOCK, A.ALLOW),
  },
};

/** (text, stage) -> text; throw `GuardrailViolation` to block. */
export type ExtraCheck = (text: string, stage: string) => string;

export interface GuardrailsOptions {
  extraChecks?: readonly ExtraCheck[];
  enabled?: boolean;
  waiver?: string | null;
}

/** Applies a profile's rules to text at each stage of an invocation. */
export class Guardrails {
  readonly profile: string;
  readonly extraChecks: readonly ExtraCheck[];
  readonly enabled: boolean;
  readonly waiver: string | null;

  constructor(profile = "standard", options: GuardrailsOptions = {}) {
    if (!(profile in PROFILES)) {
      throw new Error(`Unknown guardrail profile '${profile}'. Choose from ${JSON.stringify(Object.keys(PROFILES).sort())}.`);
    }
    this.profile = profile;
    this.extraChecks = [...(options.extraChecks ?? [])];
    this.enabled = options.enabled ?? true;
    this.waiver = options.waiver ?? null;
  }

  /** Disable guardrails. Requires an approved waiver ID from waivers.yaml (checked in CI). */
  static off(options: { waiver: string }): Guardrails {
    if (!options?.waiver || !options.waiver.trim()) {
      throw new Error("Disabling guardrails requires an approved waiver ID (see waivers.yaml).");
    }
    return new Guardrails("standard", { enabled: false, waiver: options.waiver.trim() });
  }

  get disabled(): boolean {
    return !this.enabled;
  }

  /** Return `text` (redacted where the profile says so); throw `GuardrailViolation` if it must be blocked. */
  enforce(text: string, stage: string): string {
    if (!this.enabled) return text;
    if (!(STAGES as readonly string[]).includes(stage)) throw new Error(`Unknown guardrail stage '${stage}'.`);
    const s = stage as Stage;

    const rules = PROFILES[this.profile];
    const matches = detect(text);
    const blocked = [...new Set(matches.filter((m) => rules[m.category][s] === Action.BLOCK).map((m) => m.category))].sort();
    if (blocked.length) {
      audit.emit(audit.GUARDRAIL_BLOCK, {
        outcome: "blocked",
        stage,
        profile: this.profile,
        categories: blocked.join(","),
        rules: [...new Set(matches.filter((m) => blocked.includes(m.category)).map((m) => m.rule))].sort().join(","),
      });
      throw new GuardrailViolation(stage, blocked);
    }

    const redactions = matches.filter((m) => rules[m.category][s] === Action.REDACT);
    if (redactions.length) {
      text = redact(text, redactions);
      audit.emit(audit.GUARDRAIL_REDACT, {
        outcome: "redacted",
        stage,
        profile: this.profile,
        categories: [...new Set(redactions.map((m) => m.category))].sort().join(","),
        count: redactions.length,
      });
    }

    for (const check of this.extraChecks) {
      try {
        text = check(text, stage);
      } catch (err) {
        if (err instanceof GuardrailViolation) {
          audit.emit(audit.GUARDRAIL_BLOCK, {
            outcome: "blocked",
            stage,
            profile: this.profile,
            categories: err.categories.join(","),
            rules: "external",
          });
        }
        throw err;
      }
    }
    return text;
  }

  /** Apply `enforce` to every string inside objects and arrays; other values pass through. */
  enforcePayload<T = unknown>(value: T, stage: string): T {
    if (!this.enabled) return value;
    return this.walk(value, stage) as T;
  }

  private walk(value: unknown, stage: string): unknown {
    if (typeof value === "string") return this.enforce(value, stage);
    if (Array.isArray(value)) return value.map((v) => this.walk(v, stage));
    if (value !== null && typeof value === "object" && Object.getPrototypeOf(value) === Object.prototype) {
      return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, this.walk(v, stage)]));
    }
    return value;
  }
}

/** Replace matched spans with [REDACTED:category], merging overlaps. */
export function redact(text: string, matches: readonly Match[]): string {
  const spans: [number, number, string][] = [];
  for (const m of [...matches].sort((a, b) => a.start - b.start || b.end - a.end)) {
    const last = spans[spans.length - 1];
    if (last && m.start < last[1]) last[1] = Math.max(last[1], m.end);
    else spans.push([m.start, m.end, m.category]);
  }
  for (const [start, end, category] of spans.reverse()) {
    text = `${text.slice(0, start)}[REDACTED:${category}]${text.slice(end)}`;
  }
  return text;
}
