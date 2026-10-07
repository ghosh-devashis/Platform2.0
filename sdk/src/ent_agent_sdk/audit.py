"""Audit events (SDK-13): a dedicated, structured stream for security-relevant actions.

Events: tool calls with side effects, guardrail blocks and redactions, waivers in effect. Each event carries the
agent and user identity and the trace ID, so it can be joined to traces and logs.

Audit events never contain prompt, response or tool content: only names, categories, counts and outcomes.

The stream is separate from application logs: one JSON object per line on stdout, or in the file named by
`ENT_AUDIT_LOG_PATH`. Use `add_sink()` to forward events elsewhere (a queue, a SIEM). Audit is best effort and
never fails an invocation (a failing sink is logged and skipped).
"""

from __future__ import annotations

import json
import logging
import os
import sys
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from opentelemetry import trace

from ent_agent_sdk import context

logger = logging.getLogger("ent_agent_sdk.audit")

TOOL_CALL = "tool_call"
GUARDRAIL_BLOCK = "guardrail_block"
GUARDRAIL_REDACT = "guardrail_redact"
WAIVER_IN_EFFECT = "waiver_in_effect"
TOOL_APPROVAL = "tool_approval"
BUDGET_SOFT = "budget_soft_limit"
BUDGET_EXCEEDED = "budget_exceeded"

_sinks: list[Callable[[dict[str, Any]], None]] = []
_configured = False


class _RawFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return record.getMessage()


def configure(path: str | None = None) -> None:
    """Send audit events to their own stream (file from `ENT_AUDIT_LOG_PATH`, else stdout). Idempotent."""
    global _configured
    if _configured:
        return
    path = path or os.environ.get("ENT_AUDIT_LOG_PATH")
    handler: logging.Handler = logging.FileHandler(path, encoding="utf-8") if path else logging.StreamHandler(sys.stdout)
    handler.setFormatter(_RawFormatter())
    logger.handlers[:] = [handler]
    logger.setLevel(logging.INFO)
    logger.propagate = False  # keep audit out of the application log stream
    _configured = True


def add_sink(sink: Callable[[dict[str, Any]], None]) -> None:
    """Also deliver every audit event to `sink(event)`."""
    _sinks.append(sink)


def remove_sink(sink: Callable[[dict[str, Any]], None]) -> None:
    if sink in _sinks:
        _sinks.remove(sink)


def emit(event_type: str, *, outcome: str = "success", **details: Any) -> dict[str, Any]:
    """Record one audit event and return it. Pass only names, categories, counts, never content."""
    invocation = context.current()
    event: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "stream": "audit",
        "event_type": event_type,
        "outcome": outcome,
        "agent": invocation.agent_name if invocation else details.get("agent"),
        "agent_version": invocation.agent_version if invocation else None,
        "team": invocation.team if invocation else None,
        "user_id": invocation.user_id if invocation else None,
        "session_id": invocation.session_id if invocation else None,
        "trace_id": invocation.trace_id if invocation else None,
        "details": details,
    }
    try:
        configure()
        logger.info(json.dumps(event, default=str))
        for sink in list(_sinks):
            try:
                sink(event)
            except Exception:
                logging.getLogger(__name__).exception("Audit sink failed")
        span = trace.get_current_span()
        if span.get_span_context().is_valid:
            attributes = {k: v for k, v in details.items() if isinstance(v, (str, int, float, bool))}
            span.add_event(f"audit.{event_type}", {"outcome": outcome, **attributes})
    except Exception:
        logging.getLogger(__name__).exception("Failed to record audit event %s", event_type)
    return event
