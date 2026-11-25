"""Working state for one Interviewer Agent turn.

Business state is rebuilt from PostgreSQL for every candidate message. This
graph state is transient and may carry live engine/context objects.
"""

from typing import Any, TypedDict


class TurnGraphState(TypedDict, total=False):
    # Input
    engine: Any
    turn_input: Any
    session_state: dict[str, Any]
    canary_code: str | None
    mode: str  # "rule" | "shadow" | "live"
    # guards_in / build_menu
    ctx: Any
    early_decision: Any
    allowed_actions: list[str]
    menu_reasons: dict[str, str]
    pacing: dict[str, Any]
    memory_search: Any
    # decide
    decision: Any
    rule_decision: Any
    agent_decision: Any
    executed: str
    decide_error: str | None
    # act / guards_out
    output: Any
    # record
    record: dict[str, Any]
    started_at_ms: float
