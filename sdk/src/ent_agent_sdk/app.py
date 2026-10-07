"""EnterpriseAgentApp: serves a LangGraph graph on the Bedrock AgentCore runtime contract (SDK-01).

Contract: GET /ping for health, POST /invocations for requests, port 8080. The same image runs locally and
in AgentCore (RUN-02), so nothing here is local-only. Every invocation is traced (SDK-03, observability.py),
checked by guardrails on the way in and out (SDK-08, guardrails.py) and carries the caller's identity for audit
events (SDK-13, audit.py).
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from collections.abc import AsyncGenerator, Callable, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.telemetry import TelemetryConfig
import openai
from langchain_core.messages import BaseMessage, HumanMessage
from langgraph.types import Command
from opentelemetry import propagate, trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import SpanKind

from ent_agent_sdk import audit, context, health, manifest as manifest_mod, memory, metadata
from ent_agent_sdk import observability as obs
from ent_agent_sdk.budget import BudgetExceeded, BudgetGuard, BudgetTracker, TokenBudget
from ent_agent_sdk.guardrails import Guardrails, GuardrailViolation

logger = logging.getLogger("ent_agent_sdk")

AGENTCORE_PORT = 8080
SESSION_HEADER = "x-amzn-bedrock-agentcore-runtime-session-id"
USER_HEADER = "x-amzn-bedrock-agentcore-runtime-user-id"
TRACE_ID_HEADER = "x-ent-trace-id"
FASTAPI_TELEMETRY_OFF: TelemetryConfig = {
    "tracing": False,
    "metrics": False,
    "logs": False,
    "operation_spans": False,
    "auto_configure": False,
}

InputMapper = Callable[[dict[str, Any]], dict[str, Any]]
OutputMapper = Callable[[Any], dict[str, Any]]
HealthCheck = Callable[[], bool]


def default_input_mapper(payload: dict[str, Any]) -> dict[str, Any]:
    """Standard request: {"prompt": "..."} becomes a MessagesState input. Anything else passes through."""
    if isinstance(payload.get("prompt"), str) and "messages" not in payload:
        rest = {k: v for k, v in payload.items() if k != "prompt"}
        return {"messages": [HumanMessage(content=payload["prompt"])], **rest}
    return payload


def default_output_mapper(state: Any) -> dict[str, Any]:
    """Standard response: {"output": <text of the last message>}; non-message states are returned as JSON."""
    if isinstance(state, Mapping) and state.get("messages"):
        last = state["messages"][-1]
        if isinstance(last, BaseMessage):
            content = last.content
            return {"output": content if isinstance(content, str) else to_jsonable(content)}
    jsonable = to_jsonable(state)
    return jsonable if isinstance(jsonable, dict) else {"output": jsonable}


def to_jsonable(value: Any) -> Any:
    """Convert graph state (which may hold LangChain messages) into JSON-safe data."""
    if isinstance(value, BaseMessage):
        return {"type": value.type, "content": to_jsonable(value.content)}
    if isinstance(value, Mapping):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


class EnterpriseAgentApp:
    """Wraps a compiled LangGraph graph and serves it on the AgentCore runtime contract.

    `guardrails` defaults to the `standard` profile; disabling needs `Guardrails.off(waiver=...)`.
    `health_check` (optional) is called by /ping; returning False or raising reports the agent Unhealthy
    (HTTP 503). `shutdown_timeout` bounds how long in-flight requests may finish on shutdown (SDK-16).
    `tracer_provider` is for tests and special cases; by default it is built from OTEL_* settings.
    `checkpointer` stores each session's state (SDK-11): "auto" (default) keeps the graph's own checkpointer, else
    the one named by `ENT_CHECKPOINTER`, else an in-memory one; pass a saver from `ent_agent_sdk.memory`, or None for
    stateless graphs (which also turns off sessions and approvals). Without the session header each call is
    independent; with it, the conversation continues. `token_budget` limits tokens per invocation (SDK-12).
    Use `EnterpriseAgentApp.from_manifest(...)` to take name, team and guardrail profile from `agent.yaml`.

    Human approval (SDK-10): when a tool needs approval the call returns HTTP 202
    `{"status": "approval_required", "approvals": [...], "session_id": ...}`; send the answer to the same session
    as `{"resume": {"approved": true, "approver": "alice"}}`.
    """

    def __init__(
        self,
        graph: Any,
        *,
        name: str,
        version: str = "0.0.0",
        team: str | None = None,
        data_classification: str | None = None,
        guardrails: Guardrails | None = None,
        input_mapper: InputMapper = default_input_mapper,
        output_mapper: OutputMapper = default_output_mapper,
        health_check: HealthCheck | None = None,
        shutdown_timeout: float = 30.0,
        tracer_provider: TracerProvider | None = None,
        checkpointer: Any = "auto",
        token_budget: TokenBudget | None = None,
    ) -> None:
        self.graph = graph
        self.token_budget = token_budget
        if checkpointer == "auto":
            if getattr(graph, "checkpointer", None) in (None, False):
                graph.checkpointer = memory.from_environment() or memory.in_memory()
        elif checkpointer is not None:
            graph.checkpointer = checkpointer
        self.checkpointer = getattr(graph, "checkpointer", None) or None
        self.name = name
        self.version = version
        self.team = team
        self.data_classification = data_classification
        self.guardrails = guardrails or Guardrails()
        self.input_mapper = input_mapper
        self.output_mapper = output_mapper
        self.health_check = health_check
        self.shutdown_timeout = shutdown_timeout
        self.tracer_provider = tracer_provider or obs.build_tracer_provider(
            service_name=name, service_version=version, team=team, extra_attributes=metadata.resource_attributes()
        )
        self.tracer = self.tracer_provider.get_tracer(obs.TRACER_NAME, obs.sdk_version())
        self._in_flight = 0
        self._last_status_change = int(time.time())
        if self.guardrails.disabled:
            audit.emit(audit.WAIVER_IN_EFFECT, outcome="waived", agent=name, control="guardrails",
                       waiver=self.guardrails.waiver)
        self.asgi = self._build_asgi()

    @classmethod
    def from_manifest(
        cls,
        graph: Any,
        manifest: manifest_mod.AgentManifest | str | Path = "agent.yaml",
        *,
        version: str = "0.0.0",
        **kwargs: Any,
    ) -> EnterpriseAgentApp:
        """Create the app with name, team, data classification and guardrail profile from `agent.yaml`."""
        loaded = manifest if isinstance(manifest, manifest_mod.AgentManifest) else manifest_mod.load(manifest)
        kwargs.setdefault("guardrails", Guardrails(loaded.guardrail_profile))
        kwargs.setdefault("data_classification", loaded.data_classification)
        if loaded.token_budget:
            kwargs.setdefault("token_budget", TokenBudget(**loaded.token_budget))
        if loaded.memory.get("backend") == "dynamodb":
            kwargs.setdefault("checkpointer", memory.dynamodb(loaded.memory.get("table")))
        elif loaded.memory.get("backend") == "memory":
            kwargs.setdefault("checkpointer", memory.in_memory())
        if loaded.models and os.environ.get("ENT_HEALTH_CHECK_GATEWAY", "true").lower() != "false":
            kwargs.setdefault("health_check", health.gateway_check())  # /ping reports Unhealthy if the gateway is down
        return cls(graph, name=loaded.name, version=version, team=loaded.team, **kwargs)

    def _set_in_flight(self, delta: int) -> None:
        was_busy = self._in_flight > 0
        self._in_flight += delta
        if was_busy != (self._in_flight > 0):
            self._last_status_change = int(time.time())

    def _healthy(self) -> bool:
        if self.health_check is None:
            return True
        try:
            return bool(self.health_check())
        except Exception:
            logger.exception("Health check failed for agent %s", self.name)
            return False

    def _failure(self, span: Any, exc: Exception, trace_id: str) -> JSONResponse:
        """Turn an exception from the graph into the one clear error the caller should see (details stay in logs)."""
        if _find_cause(exc, (BudgetExceeded,)) is not None:  # already audited when raised
            span.set_attribute(obs.OUTCOME, "budget_exceeded")
            return _error(429, "BudgetExceeded", "The token budget for this invocation is used up.", trace_id)
        obs.mark_error(span, exc)
        span.set_attribute(obs.OUTCOME, "error")
        if _is_gateway_failure(exc):
            logger.error("LLM gateway unavailable for agent %s: %s", self.name, type(exc).__name__)
            span.set_attribute(obs.OUTCOME, "gateway_unavailable")
            return _error(503, "GatewayUnavailable", "The LLM gateway is unavailable. Try again shortly.", trace_id)
        logger.exception("Invocation failed for agent %s", self.name)
        return _error(500, "AgentError", f"{type(exc).__name__} while running the agent.", trace_id)

    def _guard_input(self, payload: dict[str, Any]) -> dict[str, Any]:
        prompt = payload.get("prompt")
        if isinstance(prompt, str):
            return {**payload, "prompt": self.guardrails.enforce(prompt, "input")}
        return payload

    def _build_asgi(self) -> FastAPI:
        @asynccontextmanager
        async def lifespan(_: FastAPI) -> AsyncGenerator[None]:
            yield
            # Shutdown: in-flight requests have finished (or timed out); send any buffered spans.
            self.tracer_provider.shutdown()

        api = FastAPI(
            title=self.name,
            version=self.version,
            docs_url=None,
            redoc_url=None,
            lifespan=lifespan,
            # FastAPI's built-in OpenTelemetry is off: the SDK owns the trace (one coherent trace per invocation)
            # and FastAPI's version would record exception messages and export metrics on its own.
            telemetry=FASTAPI_TELEMETRY_OFF,
        )

        @api.get("/ping")
        async def ping() -> JSONResponse:
            if not self._healthy():
                return JSONResponse(
                    {"status": "Unhealthy", "time_of_last_update": int(time.time())}, status_code=503
                )
            return JSONResponse(
                {
                    "status": "HealthyBusy" if self._in_flight else "Healthy",
                    "time_of_last_update": self._last_status_change,
                }
            )

        @api.post("/invocations")
        async def invocations(request: Request) -> JSONResponse:
            try:
                payload = json.loads(await request.body() or b"{}")
            except json.JSONDecodeError:
                return _error(400, "InvalidRequest", "Request body must be valid JSON.")
            if not isinstance(payload, dict):
                return _error(400, "InvalidRequest", "Request body must be a JSON object.")

            session_id = request.headers.get(SESSION_HEADER)
            user_id = request.headers.get(USER_HEADER)
            resuming = "resume" in payload
            if resuming and (self.checkpointer is None or not session_id):
                return _error(400, "InvalidRequest", "Resuming a paused run needs the session header of the original request.")
            # With a checkpointer, a call without a session header is independent: it gets a fresh thread.
            thread_id = session_id or (f"call-{uuid.uuid4().hex}" if self.checkpointer is not None else "default")
            attributes = {
                obs.OPERATION: "invoke_agent",
                obs.AGENT_NAME: self.name,
                obs.AGENT_VERSION: self.version,
                obs.SPAN_TYPE: "agent",
            }
            if self.team:
                attributes[obs.TEAM] = self.team
            if self.data_classification:
                attributes[obs.DATA_CLASSIFICATION] = self.data_classification
            if session_id:
                attributes[obs.CONVERSATION_ID] = session_id

            # Continue the caller's trace if it sent W3C `traceparent` headers.
            caller_context = propagate.extract(request.headers)
            with self.tracer.start_as_current_span(
                f"invoke_agent {self.name}", context=caller_context, kind=SpanKind.SERVER, attributes=attributes
            ) as span:
                trace_id = format(span.get_span_context().trace_id, "032x")
                tracker = BudgetTracker(self.token_budget) if self.token_budget else None
                invocation = context.InvocationContext(
                    agent_name=self.name, agent_version=self.version, team=self.team, session_id=session_id,
                    user_id=user_id, trace_id=trace_id, guardrails=self.guardrails, budget=tracker,
                    data_classification=self.data_classification,
                )
                callbacks: list[Any] = [obs.GraphTracer(self.tracer, trace.set_span_in_context(span))]
                if tracker is not None:
                    callbacks.append(BudgetGuard(tracker))
                config = {
                    "configurable": {"thread_id": thread_id},
                    "metadata": {"agent_name": self.name, "agent_version": self.version, "session_id": session_id},
                    "callbacks": callbacks,
                }

                with context.use(invocation):
                    self._set_in_flight(+1)
                    try:
                        try:
                            graph_input = (
                                Command(resume=payload["resume"]) if resuming
                                else self.input_mapper(self._guard_input(payload))
                            )
                        except GuardrailViolation:
                            span.set_attribute(obs.OUTCOME, "blocked")
                            return _error(400, "GuardrailBlocked", "Request blocked by guardrail policy.", trace_id)

                        state = await self.graph.ainvoke(graph_input, config=config)
                        if isinstance(state, Mapping) and state.get("__interrupt__"):  # a tool is waiting for approval
                            approvals = [to_jsonable(getattr(item, "value", item)) for item in state["__interrupt__"]]
                            span.set_attribute(obs.OUTCOME, "pending_approval")
                            return JSONResponse(
                                {"status": "approval_required", "approvals": approvals, "session_id": thread_id},
                                status_code=202, headers={TRACE_ID_HEADER: trace_id},
                            )
                        body = self.output_mapper(state)
                        try:
                            body = self.guardrails.enforce_payload(body, "output")
                        except GuardrailViolation:
                            span.set_attribute(obs.OUTCOME, "blocked")
                            return _error(502, "GuardrailBlocked", "Response withheld by guardrail policy.", trace_id)
                    except GuardrailViolation:  # raised inside a tool and not handled by the graph
                        span.set_attribute(obs.OUTCOME, "blocked")
                        return _error(502, "GuardrailBlocked", "Response withheld by guardrail policy.", trace_id)
                    except Exception as exc:  # the contract must always answer; details go to logs, not callers
                        return self._failure(span, exc, trace_id)
                    finally:
                        self._set_in_flight(-1)
                        if tracker is not None:
                            span.set_attribute(obs.INPUT_TOKENS, tracker.input_tokens)
                            span.set_attribute(obs.OUTPUT_TOKENS, tracker.output_tokens)
                span.set_attribute(obs.OUTCOME, "success")

            if session_id:
                body = {**body, "session_id": session_id}
            return JSONResponse(body, headers={TRACE_ID_HEADER: trace_id})

        return api

    def run(self, host: str = "0.0.0.0", port: int | None = None) -> None:
        """Serve the agent with JSON logs. AgentCore requires port 8080; `PORT` overrides it for local runs."""
        port = port or int(os.environ.get("PORT", AGENTCORE_PORT))
        obs.configure_logging(self.name)
        logger.info("Starting %s %s on %s:%s", self.name, self.version, host, port)
        # log_config=None: uvicorn's own logs go through the JSON handler too.
        uvicorn.run(
            self.asgi, host=host, port=port, log_config=None, timeout_graceful_shutdown=self.shutdown_timeout
        )


def _find_cause(exc: BaseException, types: tuple[type[BaseException], ...]) -> BaseException | None:
    """The first exception of `types` in `exc`'s cause/context chain (libraries often wrap the real error)."""
    seen: set[int] = set()
    stack: list[BaseException | None] = [exc]
    while stack:
        current = stack.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, types):
            return current
        stack.extend((current.__cause__, current.__context__))
    return None


def _is_gateway_failure(exc: BaseException) -> bool:
    """True when the LLM gateway could not be reached or answered 502/503/504."""
    if _find_cause(exc, (openai.APIConnectionError,)) is not None:  # includes timeouts
        return True
    status = _find_cause(exc, (openai.APIStatusError,))
    return status is not None and getattr(status, "status_code", 0) in (502, 503, 504)


def _error(status: int, error_type: str, message: str, trace_id: str | None = None) -> JSONResponse:
    headers = {TRACE_ID_HEADER: trace_id} if trace_id else None
    return JSONResponse({"error": {"type": error_type, "message": message}}, status_code=status, headers=headers)
