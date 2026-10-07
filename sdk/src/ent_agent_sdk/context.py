"""Per-invocation context: who is calling which agent, in which session, under which guardrails.

Set by `EnterpriseAgentApp` for the duration of each invocation. Audit events, tool wrappers and guardrails read
it, so agent code never has to pass identity or policy around.
"""

from __future__ import annotations

import contextvars
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class InvocationContext:
    agent_name: str
    agent_version: str
    team: str | None = None
    session_id: str | None = None
    user_id: str | None = None
    trace_id: str | None = None
    guardrails: Any = None  # ent_agent_sdk.guardrails.Guardrails (typed loosely to avoid an import cycle)
    budget: Any = None  # ent_agent_sdk.budget.BudgetTracker for this invocation
    data_classification: str | None = None  # from the manifest (public/internal/confidential/restricted)


_current: contextvars.ContextVar[InvocationContext | None] = contextvars.ContextVar("ent_invocation", default=None)


def current() -> InvocationContext | None:
    """The context of the invocation being served, or None outside one."""
    return _current.get()


@contextmanager
def use(invocation: InvocationContext) -> Iterator[InvocationContext]:
    token = _current.set(invocation)
    try:
        yield invocation
    finally:
        _current.reset(token)
