"""Token budgets per invocation (SDK-12): a soft limit that warns and a hard limit that stops the run.

    EnterpriseAgentApp(graph, name="a", token_budget=TokenBudget(soft=20_000, hard=50_000))
    # or in agent.yaml:   token_budget: {soft: 20000, hard: 50000}

Tokens are counted from the usage the model reports on each call (input + output). When the total reaches the
soft limit an audit event and a trace event are recorded once; when it reaches the hard limit the *next* model call
is refused with `BudgetExceeded` and the caller gets HTTP 429 `BudgetExceeded`. A single very large model call can
overshoot the hard limit, because the limit can only be checked between calls. Streaming calls that report no usage
are not counted.

This is the SDK-side counter. Per-team/day budgets across agents belong to the gateway (see the model router's
budgets, GW-07).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from langchain_core.callbacks import BaseCallbackHandler

from ent_agent_sdk import audit


class BudgetExceeded(RuntimeError):
    """The invocation used up its hard token budget."""

    def __init__(self, used: int, hard: int) -> None:
        self.used, self.hard = used, hard
        super().__init__(f"Token budget exceeded ({used} of {hard} tokens used).")


@dataclass(frozen=True)
class TokenBudget:
    soft: int | None = None
    hard: int | None = None

    def __post_init__(self) -> None:
        for name, value in (("soft", self.soft), ("hard", self.hard)):
            if value is not None and value <= 0:
                raise ValueError(f"token budget '{name}' must be positive.")
        if self.soft is not None and self.hard is not None and self.soft > self.hard:
            raise ValueError("token budget 'soft' must not exceed 'hard'.")


class BudgetTracker:
    """Counts tokens for one invocation."""

    def __init__(self, budget: TokenBudget) -> None:
        self.budget = budget
        self.input_tokens = 0
        self.output_tokens = 0
        self._soft_reported = False
        self._lock = threading.Lock()

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens

    def add(self, input_tokens: int, output_tokens: int) -> None:
        with self._lock:
            self.input_tokens += input_tokens
            self.output_tokens += output_tokens
            report = bool(self.budget.soft and self.total >= self.budget.soft and not self._soft_reported)
            self._soft_reported = self._soft_reported or report
            total = self.total
        if report:
            audit.emit(audit.BUDGET_SOFT, outcome="warned", tokens_used=total, soft_limit=self.budget.soft,
                       hard_limit=self.budget.hard)

    def check(self) -> None:
        """Raise `BudgetExceeded` if the hard limit has been reached."""
        hard = self.budget.hard
        if hard is not None and self.total >= hard:
            audit.emit(audit.BUDGET_EXCEEDED, outcome="blocked", tokens_used=self.total, hard_limit=hard)
            raise BudgetExceeded(self.total, hard)


class BudgetGuard(BaseCallbackHandler):
    """Refuses a model call once the hard budget is spent. The exception type is preserved (`raise_error`)."""

    raise_error = True
    run_inline = True

    def __init__(self, tracker: BudgetTracker) -> None:
        self.tracker = tracker

    def on_chat_model_start(self, serialized, messages, **kwargs):  # noqa: ANN001, ANN201
        self.tracker.check()

    def on_llm_start(self, serialized, prompts, **kwargs):  # noqa: ANN001, ANN201
        self.tracker.check()
