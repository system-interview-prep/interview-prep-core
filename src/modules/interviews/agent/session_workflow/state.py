"""Serializable state for the session command workflow.

Business state remains in PostgreSQL; a command thread only records execution
of one command. Never put a database session or a provider client in
``InterviewGraphState``.

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
