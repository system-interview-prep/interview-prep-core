"""A hard cap on provider calls, charged where every call is dispatched.

The §13 pilot found a session spending four times its attributed budget on
speculative previews that no counter saw. Counting after the fact (the
``ai_call_logs`` table) cannot stop anything; this module charges *before*
the HTTP request leaves, once per attempt, so retries, option-drop retries,
fallback speech calls and speculation all count against the same numbers.

Two numbers, both from settings:

* **per scope** -- one candidate turn, one speculative preview, one
  deterministic prefetch (limit 0: a provider call there is a bug);
* **per session** -- every call charged under any scope of that session in
  this process, so a session cannot run away across many requests.

A refused call raises :class:`CallBudgetExceeded` without touching the
provider and writes an ``ai_call_logs`` row with status ``refused``, so the
admin view and the §13 report can see it. Callers that already fall back on
provider errors (the model decider, the output guards) fall back on this one
the same way; a call with no scope and no session is not charged, because
the matching and CV pipelines use the same facade outside any interview.

The session ledger is per process. Across API replicas each replica keeps
its own count; the per-scope cap is exact everywhere.
"""

from __future__ import annotations

import contextlib
import contextvars
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field

#: Session ledger entries idle this long are forgotten (an interview is 25 min).
_LEDGER_TTL_SECONDS = 6 * 3600


class CallBudgetExceeded(RuntimeError):
    """The scope or the session has no provider calls left."""

    def __init__(self, *, scope: str, level: str, limit: int, spent: int, purpose: str) -> None:
        self.scope = scope
        self.level = level
        self.limit = limit
        self.spent = spent
        self.purpose = purpose
        super().__init__(f"{level} call budget exhausted in {scope}: {spent}/{limit} before {purpose}")


@dataclass
class CallBudget:
    """What one scope may spend, and what it has spent."""

    scope: str
    limit: int
    session_id: str | None = None
    session_limit: int | None = None
    spent: int = 0
    charged: list[str] = field(default_factory=list)

    def remaining(self) -> int:
        return max(0, self.limit - self.spent)


class _SessionLedger:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._spent: dict[str, tuple[int, float]] = {}

    def charge(self, session_id: str, limit: int, *, scope: str, purpose: str) -> int:
        now = time.monotonic()
        with self._lock:
            if len(self._spent) > 256:
                stale = [k for k, (_, seen) in self._spent.items() if now - seen > _LEDGER_TTL_SECONDS]
                for key in stale:
                    del self._spent[key]
            count, _ = self._spent.get(session_id, (0, now))
            if count >= limit:
                raise CallBudgetExceeded(scope=scope, level="session", limit=limit, spent=count, purpose=purpose)
            self._spent[session_id] = (count + 1, now)
            return count + 1

    def spent(self, session_id: str) -> int:
        with self._lock:
            return self._spent.get(session_id, (0, 0.0))[0]

    def reset(self, session_id: str | None = None) -> None:
        with self._lock:
            if session_id is None:
                self._spent.clear()
            else:
                self._spent.pop(session_id, None)


_ledger = _SessionLedger()

current_call_budget: contextvars.ContextVar[CallBudget | None] = contextvars.ContextVar(
    "ai_call_budget", default=None
)


def charge(purpose: str) -> CallBudget | None:
    """Charge one dispatch attempt to the active scope; raise when none is left.

    Called by the facade immediately before each provider request, including
    the retry of a refused option and the retry of a transport error. Returns
    the budget it charged, or ``None`` when no scope is active.
    """
    budget = current_call_budget.get()
    if budget is None:
        return None
    if budget.spent >= budget.limit:
        raise CallBudgetExceeded(
            scope=budget.scope, level="scope", limit=budget.limit, spent=budget.spent, purpose=purpose
        )
    if budget.session_id and budget.session_limit:
        _ledger.charge(budget.session_id, budget.session_limit, scope=budget.scope, purpose=purpose)
    budget.spent += 1
    budget.charged.append(purpose)
    return budget


@contextlib.contextmanager
def budget_scope(
    scope: str, *, limit: int, session_id: str | None = None, session_limit: int | None = None
) -> Iterator[CallBudget]:
    """Run a block under a call budget. Nested scopes replace, never add."""
    budget = CallBudget(scope=scope, limit=max(0, int(limit)), session_id=session_id, session_limit=session_limit)
    token = current_call_budget.set(budget)
    try:
        yield budget
    finally:
        current_call_budget.reset(token)


def session_spent(session_id: str) -> int:
    """Calls charged to a session in this process so far."""
    return _ledger.spent(session_id)


def reset_session_ledger(session_id: str | None = None) -> None:
    """Forget a session's count (tests, or an operator resetting a session)."""
    _ledger.reset(session_id)
