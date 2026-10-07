"""Tool registry and allowlist (SDK-09), with guardrails, audit and resilience built in.

    from ent_agent_sdk import tools

    @tools.tool(owner="crm-team", data_classification="confidential", side_effects=False)
    def lookup_customer(customer_id: str) -> str:
        \"\"\"Look up a customer by ID.\"\"\"
        ...

    llm = tools.bind(get_model("chat-default"), [lookup_customer])   # fails if a tool isn't registered

Every registered tool:
- carries metadata (owner, data classification, side effects) that appears on its trace span;
- has its arguments and result checked by the guardrails of the running invocation (stages `tool_input` and
  `tool_output`);
- has a timeout, and read-only tools get a bounded retry with backoff (SDK-16). Tools with side effects are never
  retried automatically, and every call to one is written to the audit stream (SDK-13);
- reports failures as `ToolError` carrying the exception type only; details go to the log.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import inspect
import logging
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool
from langgraph.errors import GraphBubbleUp
from langgraph.types import interrupt

from ent_agent_sdk import audit, context
from ent_agent_sdk.guardrails import Guardrails, GuardrailViolation

logger = logging.getLogger("ent_agent_sdk.tools")

DATA_CLASSIFICATIONS = ("public", "internal", "confidential", "restricted")
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_READ_RETRIES = 1

_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=32, thread_name_prefix="ent-tool")


class ToolError(RuntimeError):
    """A tool failed. The message names the tool and the exception type, never the underlying message."""


class ToolTimeoutError(ToolError):
    """A tool did not finish within its timeout."""


class UnregisteredToolError(ToolError):
    """A tool that is not in the registry was bound or used."""


@dataclass(frozen=True)
class ToolMetadata:
    name: str
    owner: str
    data_classification: str
    side_effects: bool
    timeout_seconds: float
    retries: int
    requires_approval: bool = False


def _backoff(attempt: int) -> float:
    return min(0.2 * (2**attempt), 2.0)


def _guardrails() -> Guardrails:
    invocation = context.current()
    return (invocation.guardrails if invocation and invocation.guardrails else None) or Guardrails()


def _audit(meta: ToolMetadata, outcome: str, **extra: Any) -> None:
    if meta.side_effects:
        audit.emit(
            audit.TOOL_CALL, outcome=outcome, tool=meta.name, owner=meta.owner,
            data_classification=meta.data_classification, side_effects=True, **extra,
        )


def _fail(meta: ToolMetadata, exc: BaseException) -> ToolError:
    logger.error("Tool %s failed: %s", meta.name, type(exc).__name__, exc_info=exc)
    _audit(meta, "failure", error_type=type(exc).__name__)
    if isinstance(exc, ToolError):
        return exc
    return ToolError(f"Tool '{meta.name}' failed ({type(exc).__name__}).")


def _approve(meta: ToolMetadata, arguments: dict[str, Any]) -> str | None:
    """Pause for human approval (SDK-10): the graph is interrupted, the caller sees the request, and the run
    continues only when it resumes with `{"approved": true}`. Needs a checkpointer and a session (the app provides both).

    Returns None when approved (or no approval is needed), else the message to hand back to the model: a declined
    action is a normal outcome the agent can explain, not an error."""
    if not meta.requires_approval:
        return None
    request = {
        "type": "tool_approval", "tool": meta.name, "owner": meta.owner,
        "data_classification": meta.data_classification, "arguments": arguments,
    }
    try:
        decision = interrupt(request)  # first pass: raises GraphInterrupt; on resume: returns the caller's answer
    except GraphBubbleUp:
        audit.emit(audit.TOOL_APPROVAL, outcome="requested", tool=meta.name, owner=meta.owner,
                   data_classification=meta.data_classification)
        raise
    approved = decision is True or (isinstance(decision, dict) and decision.get("approved") is True)
    approver = decision.get("approver") if isinstance(decision, dict) and isinstance(decision.get("approver"), str) else None
    audit.emit(audit.TOOL_APPROVAL, outcome="approved" if approved else "rejected", tool=meta.name, approver=approver)
    if not approved:
        return f"Not approved: a human reviewer declined '{meta.name}'. The action was not performed; don't retry it."
    return None


def _call_sync(meta: ToolMetadata, func: Callable[..., Any], kwargs: dict[str, Any]) -> Any:
    guardrails = _guardrails()
    safe = guardrails.enforce_payload(kwargs, "tool_input")
    declined = _approve(meta, safe)
    if declined is not None:
        return declined
    _audit(meta, "started")
    error: BaseException | None = None
    for attempt in range(meta.retries + 1):
        future = _POOL.submit(contextvars.copy_context().run, func, **safe)
        try:
            result = future.result(timeout=meta.timeout_seconds)
        except TimeoutError:
            future.cancel()
            error = ToolTimeoutError(f"Tool '{meta.name}' timed out after {meta.timeout_seconds:g}s.")
        except GuardrailViolation:
            raise
        except Exception as exc:
            error = exc
        else:
            result = guardrails.enforce_payload(result, "tool_output")
            _audit(meta, "success")
            return result
        if attempt < meta.retries:
            time.sleep(_backoff(attempt))
    assert error is not None
    raise _fail(meta, error) from None


async def _call_async(meta: ToolMetadata, func: Callable[..., Any], kwargs: dict[str, Any]) -> Any:
    guardrails = _guardrails()
    safe = guardrails.enforce_payload(kwargs, "tool_input")
    declined = _approve(meta, safe)
    if declined is not None:
        return declined
    _audit(meta, "started")
    error: BaseException | None = None
    for attempt in range(meta.retries + 1):
        try:
            if inspect.iscoroutinefunction(func):
                result = await asyncio.wait_for(func(**safe), meta.timeout_seconds)
            else:
                result = await asyncio.wait_for(asyncio.to_thread(func, **safe), meta.timeout_seconds)
        except TimeoutError:
            error = ToolTimeoutError(f"Tool '{meta.name}' timed out after {meta.timeout_seconds:g}s.")
        except GuardrailViolation:
            raise
        except Exception as exc:
            error = exc
        else:
            result = guardrails.enforce_payload(result, "tool_output")
            _audit(meta, "success")
            return result
        if attempt < meta.retries:
            await asyncio.sleep(_backoff(attempt))
    assert error is not None
    raise _fail(meta, error) from None


class ToolRegistry:
    """The approved tools of an agent. Only registered tools can be bound to a model."""

    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}
        self._metadata: dict[str, ToolMetadata] = {}

    def tool(
        self,
        *,
        owner: str,
        data_classification: str = "internal",
        side_effects: bool = False,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        retries: int | None = None,
        name: str | None = None,
        requires_approval: bool | None = None,
        approval_waiver: str | None = None,
    ) -> Callable[[Callable[..., Any]], BaseTool]:
        """Decorator: register a function as a tool. `retries` applies to read-only tools only.

        Tools with `side_effects=True` pause for human approval before running (SDK-10). Turning that off needs an
        approved waiver ID: `requires_approval=False, approval_waiver="WAIVER-123"`.
        """
        if not owner or not owner.strip():
            raise ValueError("A tool needs an owner (team or person responsible for it).")
        if data_classification not in DATA_CLASSIFICATIONS:
            raise ValueError(f"data_classification must be one of {DATA_CLASSIFICATIONS}.")
        if side_effects and retries:
            raise ValueError("Tools with side effects are never retried automatically; remove `retries`.")
        if requires_approval and not side_effects:
            raise ValueError("requires_approval only applies to tools with side_effects=True.")
        needs_approval = side_effects if requires_approval is None else bool(requires_approval)
        if side_effects and not needs_approval and not (approval_waiver and approval_waiver.strip()):
            raise ValueError("A side-effect tool without approval needs an approved waiver ID: approval_waiver='WAIVER-...'.")

        def decorator(func: Callable[..., Any]) -> BaseTool:
            tool_name = name or func.__name__
            if tool_name in self._tools:
                raise ValueError(f"A tool named '{tool_name}' is already registered.")
            meta = ToolMetadata(
                name=tool_name, owner=owner.strip(), data_classification=data_classification,
                side_effects=side_effects, timeout_seconds=timeout,
                retries=0 if side_effects else (DEFAULT_READ_RETRIES if retries is None else retries),
                requires_approval=needs_approval,
            )
            if side_effects and not needs_approval:
                audit.emit(audit.WAIVER_IN_EFFECT, outcome="waived", agent=None, control="tool_approval", tool=tool_name,
                           waiver=approval_waiver)
            base = StructuredTool.from_function(func, name=tool_name)  # infers description and argument schema

            def run_sync(**kwargs: Any) -> Any:
                return _call_sync(meta, func, kwargs)

            async def run_async(**kwargs: Any) -> Any:
                return await _call_async(meta, func, kwargs)

            registered = StructuredTool(
                name=tool_name,
                description=base.description,
                args_schema=base.args_schema,
                func=None if inspect.iscoroutinefunction(func) else run_sync,
                coroutine=run_async,
                metadata={
                    "ent_registered": True,
                    "ent_tool_owner": meta.owner,
                    "ent_tool_classification": meta.data_classification,
                    "ent_tool_side_effects": meta.side_effects,
                    "ent_tool_requires_approval": meta.requires_approval,
                },
            )
            self._tools[tool_name] = registered
            self._metadata[tool_name] = meta
            return registered

        return decorator

    def get(self, name: str) -> BaseTool:
        try:
            return self._tools[name]
        except KeyError:
            raise UnregisteredToolError(f"Tool '{name}' is not registered.") from None

    def metadata(self, name: str) -> ToolMetadata:
        self.get(name)
        return self._metadata[name]

    def names(self) -> list[str]:
        return sorted(self._tools)

    def all(self) -> list[BaseTool]:
        return [self._tools[n] for n in self.names()]

    def require_registered(self, tools: Iterable[BaseTool]) -> list[BaseTool]:
        """Return `tools` as a list, or raise `UnregisteredToolError` if any is not in this registry."""
        checked = []
        for candidate in tools:
            if self._tools.get(getattr(candidate, "name", None)) is not candidate:
                raise UnregisteredToolError(
                    f"Tool '{getattr(candidate, 'name', candidate)}' is not registered. "
                    "Declare it with @tools.tool(owner=..., data_classification=...)."
                )
            checked.append(candidate)
        return checked

    def bind(self, model: Any, tools: Iterable[BaseTool] | None = None) -> Any:
        """`model.bind_tools(...)` for registered tools only (all registered tools if `tools` is omitted)."""
        return model.bind_tools(self.require_registered(self.all() if tools is None else tools))


default_registry = ToolRegistry()
tool = default_registry.tool
bind = default_registry.bind
get = default_registry.get
require_registered = default_registry.require_registered
