"""Observability (SDK-03, SDK-04): OpenTelemetry traces and JSON logs for every agent, with no developer code.

Each invocation produces one trace:

    invoke_agent <agent>          (server span: agent name/version/team, session, outcome)
      └─ <graph node>             (one span per LangGraph node and nested chain)
           ├─ chat <model>        (LLM calls: model, provider, token usage)
           └─ execute_tool <name> (tool calls)

Attribute names follow the OpenTelemetry GenAI semantic conventions. Each span also carries
`fiddler.span.type` (agent/chain/llm/tool), so traces can be sent to Fiddler without code changes.
Prompt and response *content* is never recorded (it may hold personal or confidential data).

Where traces go is configuration only (SDK-04), using the standard OpenTelemetry settings:
- `OTEL_EXPORTER_OTLP_ENDPOINT` (e.g. http://localhost:4318 for the local Jaeger); unset = no export.
- `OTEL_EXPORTER_OTLP_HEADERS` for backends that need auth; `OTEL_RESOURCE_ATTRIBUTES` for extra tags
  (e.g. `deployment.environment=dev`); `OTEL_SDK_DISABLED=true` turns tracing off.

Telemetry fails open (NFR-03): export or instrumentation errors are logged and never fail an invocation.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from typing import Any
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from langgraph.errors import GraphBubbleUp
from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Span, Status, StatusCode

from ent_agent_sdk import context

logger = logging.getLogger("ent_agent_sdk.observability")

TRACER_NAME = "ent_agent_sdk"
HIDDEN_TAG = "langsmith:hidden"  # LangGraph tags its internal plumbing runs with this

# Attribute names (OpenTelemetry GenAI semantic conventions, plus ent.* for enterprise fields).
OPERATION = "gen_ai.operation.name"
AGENT_NAME = "gen_ai.agent.name"
CONVERSATION_ID = "gen_ai.conversation.id"
REQUEST_MODEL = "gen_ai.request.model"
RESPONSE_MODEL = "gen_ai.response.model"
PROVIDER = "gen_ai.provider.name"
INPUT_TOKENS = "gen_ai.usage.input_tokens"
OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
TOOL_NAME = "gen_ai.tool.name"
ERROR_TYPE = "error.type"
AGENT_VERSION = "ent.agent.version"
TEAM = "ent.team"
OUTCOME = "ent.outcome"
NODE = "ent.graph.node"
SPAN_TYPE = "fiddler.span.type"
DATA_CLASSIFICATION = "ent.data_classification"
TOOL_OWNER = "ent.tool.owner"
TOOL_CLASSIFICATION = "ent.tool.data_classification"
TOOL_SIDE_EFFECTS = "ent.tool.side_effects"


def sdk_version() -> str:
    try:
        return version("ent-agent-sdk")
    except PackageNotFoundError:
        return "unknown"


def build_tracer_provider(
    *, service_name: str, service_version: str, team: str | None = None, extra_attributes: dict[str, str] | None = None
) -> TracerProvider:
    """Create the agent's tracer provider; the exporter is chosen from standard OTEL_* settings."""
    attributes = {"service.name": service_name, "service.version": service_version, "ent.sdk.version": sdk_version()}
    if team:
        attributes[TEAM] = team
    attributes.update(extra_attributes or {})
    provider = TracerProvider(resource=Resource.create(attributes))

    if os.environ.get("OTEL_SDK_DISABLED", "").strip().lower() == "true":
        logger.info("Tracing disabled (OTEL_SDK_DISABLED=true)")
    elif os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT") or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    else:
        logger.info("No OTEL_EXPORTER_OTLP_ENDPOINT set; traces are not exported")

    # Make it the global provider too, so spans agents create themselves land in the same trace.
    if isinstance(trace.get_tracer_provider(), trace.ProxyTracerProvider):
        trace.set_tracer_provider(provider)
    return provider


class GraphTracer(BaseCallbackHandler):
    """LangChain callback handler that turns graph, node, LLM and tool runs into OpenTelemetry spans.

    One instance per invocation. `parent` is the context holding the invocation's server span.
    """

    run_inline = True  # run callbacks in the graph's own thread/task, in order

    def __init__(self, tracer: trace.Tracer, parent: Context) -> None:
        self._tracer = tracer
        self._parent = parent
        self._spans: dict[UUID, Span] = {}
        self._contexts: dict[UUID, Context] = {}
        self._lock = threading.Lock()

    # Chains: the graph itself, its nodes, and runnables inside nodes.
    def on_chain_start(self, serialized, inputs, *, run_id, parent_run_id=None, tags=None, metadata=None, **kwargs):
        if parent_run_id is None or HIDDEN_TAG in (tags or []):
            # Top-level graph run (the server span already covers it) or internal plumbing: no span of
            # its own; anything inside it attaches to the nearest visible parent.
            self._passthrough(run_id, parent_run_id)
            return
        name = kwargs.get("name") or _serialized_name(serialized) or "chain"
        attributes = {SPAN_TYPE: "chain"}
        node = (metadata or {}).get("langgraph_node")
        if node:
            attributes[NODE] = node
        self._start(run_id, parent_run_id, name, attributes)

    def on_chain_end(self, outputs, *, run_id, **kwargs):
        self._end(run_id)

    def on_chain_error(self, error, *, run_id, **kwargs):
        self._end(run_id, error)

    # LLM calls.
    def on_chat_model_start(self, serialized, messages, *, run_id, parent_run_id=None, metadata=None, **kwargs):
        self._start_llm(serialized, run_id, parent_run_id, metadata, kwargs)

    def on_llm_start(self, serialized, prompts, *, run_id, parent_run_id=None, metadata=None, **kwargs):
        self._start_llm(serialized, run_id, parent_run_id, metadata, kwargs)

    def on_llm_end(self, response, *, run_id, **kwargs):
        input_tokens = output_tokens = 0
        response_model = None
        for generations in response.generations:
            for generation in generations:
                message = getattr(generation, "message", None)
                usage = getattr(message, "usage_metadata", None) or {}
                input_tokens += usage.get("input_tokens", 0)
                output_tokens += usage.get("output_tokens", 0)
                response_model = response_model or (getattr(message, "response_metadata", None) or {}).get(
                    "model_name"
                )
        attributes: dict[str, Any] = {}
        if input_tokens or output_tokens:
            attributes.update({INPUT_TOKENS: input_tokens, OUTPUT_TOKENS: output_tokens})
            invocation = context.current()
            if invocation is not None and invocation.budget is not None:  # SDK-12: count toward the token budget
                invocation.budget.add(input_tokens, output_tokens)
        if response_model:
            attributes[RESPONSE_MODEL] = response_model
        self._end(run_id, attributes=attributes)

    def on_llm_error(self, error, *, run_id, **kwargs):
        self._end(run_id, error)

    # Tool calls.
    def on_tool_start(self, serialized, input_str, *, run_id, parent_run_id=None, metadata=None, **kwargs):
        name = kwargs.get("name") or _serialized_name(serialized) or "tool"
        attributes = {OPERATION: "execute_tool", TOOL_NAME: name, SPAN_TYPE: "tool"}
        meta = metadata or {}
        if meta.get("ent_tool_owner"):  # registered tools (tools.py) describe themselves
            attributes[TOOL_OWNER] = meta["ent_tool_owner"]
            attributes[TOOL_CLASSIFICATION] = meta.get("ent_tool_classification", "unknown")
            attributes[TOOL_SIDE_EFFECTS] = bool(meta.get("ent_tool_side_effects", False))
        self._start(run_id, parent_run_id, f"execute_tool {name}", attributes)

    def on_tool_end(self, output, *, run_id, **kwargs):
        self._end(run_id)

    def on_tool_error(self, error, *, run_id, **kwargs):
        self._end(run_id, error)

    # Helpers.
    def _start_llm(self, serialized, run_id, parent_run_id, metadata, kwargs) -> None:
        metadata = metadata or {}
        model = metadata.get("ls_model_name") or kwargs.get("name") or _serialized_name(serialized) or "unknown"
        attributes = {OPERATION: "chat", REQUEST_MODEL: model, SPAN_TYPE: "llm"}
        if metadata.get("ls_provider"):
            attributes[PROVIDER] = metadata["ls_provider"]
        self._start(run_id, parent_run_id, f"chat {model}", attributes)

    def _parent_context(self, parent_run_id: UUID | None) -> Context:
        with self._lock:
            return self._contexts.get(parent_run_id, self._parent) if parent_run_id else self._parent

    def _passthrough(self, run_id: UUID, parent_run_id: UUID | None) -> None:
        context = self._parent_context(parent_run_id)
        with self._lock:
            self._contexts[run_id] = context

    def _start(self, run_id: UUID, parent_run_id: UUID | None, name: str, attributes: dict[str, Any]) -> None:
        context = self._parent_context(parent_run_id)
        span = self._tracer.start_span(name, context=context, attributes=attributes)
        with self._lock:
            self._spans[run_id] = span
            self._contexts[run_id] = trace.set_span_in_context(span, context)

    def _end(self, run_id: UUID, error: BaseException | None = None, attributes: dict[str, Any] | None = None):
        with self._lock:
            self._contexts.pop(run_id, None)
            span = self._spans.pop(run_id, None)
        if span is None:
            return
        if attributes:
            span.set_attributes(attributes)
        if error is not None and not isinstance(error, GraphBubbleUp):  # interrupts are control flow
            mark_error(span, error)
        span.end()


def mark_error(span: Span, error: BaseException) -> None:
    """Record a failure on a span: the error type only, never the message (it may contain data)."""
    span.set_attribute(ERROR_TYPE, type(error).__name__)
    span.set_status(Status(StatusCode.ERROR, type(error).__name__))


def _serialized_name(serialized: Any) -> str | None:
    if isinstance(serialized, dict):
        return serialized.get("name") or (serialized.get("id") or [None])[-1]
    return None


class JsonLogFormatter(logging.Formatter):
    """One JSON object per log line, with the current trace and span IDs for correlation (SDK-03)."""

    def __init__(self, service_name: str | None = None) -> None:
        super().__init__()
        self.service_name = service_name

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if self.service_name:
            entry["service"] = self.service_name
        span_context = trace.get_current_span().get_span_context()
        if span_context.is_valid:
            entry["trace_id"] = format(span_context.trace_id, "032x")
            entry["span_id"] = format(span_context.span_id, "016x")
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def configure_logging(service_name: str | None = None) -> None:
    """Send all logs (agent, SDK, uvicorn) to stdout as JSON. Level from `LOG_LEVEL` (default INFO)."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonLogFormatter(service_name))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())
