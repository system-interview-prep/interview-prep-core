"""Serializable state for one interview command, and the state of one turn.

Business state remains in PostgreSQL; a command thread only records execution
of one command. Never put a database session or a provider client in
``InterviewGraphState``.

``TurnGraphState`` is the working memory of one candidate turn inside the turn
graph (``turn_graph.py``): it is not checkpointed in Pha 1 and carries live
objects (the engine, the ``TurnContext``); it is rebuilt from the database for
every turn by ``application/runtime/chat_runtime.py``.
"""

from typing import Any, Literal, TypedDict

InterviewCommand = Literal["prepare", "freeze", "open", "respond", "finish", "score"]


class InterviewGraphState(TypedDict, total=False):
    command: InterviewCommand
    session_id: str
    event_id: str
    payload: dict[str, Any]
    result: dict[str, Any]
    completed: bool


class TurnGraphState(TypedDict, total=False):
    # in
    engine: Any
    turn_input: Any
    session_state: dict[str, Any]
    canary_code: str | None
    mode: str  # "rule" (Pha 1) | "shadow" | "live" (Pha 2+)
    # guards_in / build_menu
    ctx: Any
    early_decision: Any
    allowed_actions: list[str]
    menu_reasons: dict[str, str]
    pacing: dict[str, Any]
    memory_search: Any
    # decide
    decision: Any            # what act executes
    rule_decision: Any       # the rule ladder's choice (always made)
    agent_decision: Any      # the model's choice (shadow / live), or None
    executed: str            # "rule" | "agent"
    decide_error: str | None
    # act / guards_out
    output: Any
    # record
    record: dict[str, Any]
    started_at_ms: float
