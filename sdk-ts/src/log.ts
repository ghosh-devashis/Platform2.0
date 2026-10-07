/** JSON logs with trace ids (SDK-03). One JSON object per line; level from `LOG_LEVEL`. */

import { trace } from "@opentelemetry/api";

const LEVELS: Record<string, number> = { DEBUG: 10, INFO: 20, WARNING: 30, ERROR: 40, CRITICAL: 50 };
let configured = false;
let service: string | undefined;

function threshold(): number {
  const raw = (process.env.LOG_LEVEL ?? (configured ? "INFO" : "WARNING")).toUpperCase();
  return LEVELS[raw === "WARN" ? "WARNING" : raw] ?? LEVELS.INFO;
}

/** Send logs to stdout as JSON (called by `app.run()`). Level from `LOG_LEVEL` (default INFO). */
export function configureLogging(serviceName?: string): void {
  configured = true;
  service = serviceName;
}

export function formatLog(level: string, logger: string, message: string, error?: unknown): string {
  const entry: Record<string, unknown> = {
    timestamp: new Date().toISOString(),
    level,
    logger,
    message,
  };
  if (service) entry.service = service;
  const spanContext = trace.getActiveSpan()?.spanContext();
  if (spanContext && trace.isSpanContextValid(spanContext)) {
    entry.trace_id = spanContext.traceId;
    entry.span_id = spanContext.spanId;
  }
  if (error !== undefined) entry.exception = error instanceof Error ? (error.stack ?? error.name) : String(error);
  return JSON.stringify(entry);
}

export interface Logger {
  debug(message: string): void;
  info(message: string): void;
  warning(message: string, error?: unknown): void;
  error(message: string, error?: unknown): void;
}

export function getLogger(name: string): Logger {
  const emit = (level: string, message: string, error?: unknown): void => {
    if ((LEVELS[level] ?? 20) < threshold()) return;
    const line = formatLog(level, name, message, error) + "\n";
    (configured ? process.stdout : process.stderr).write(line);
  };
  return {
    debug: (m) => emit("DEBUG", m),
    info: (m) => emit("INFO", m),
    warning: (m, e) => emit("WARNING", m, e),
    error: (m, e) => emit("ERROR", m, e),
  };
}
