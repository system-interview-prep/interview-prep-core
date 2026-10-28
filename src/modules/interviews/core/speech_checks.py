"""What the speech guards did in one interviewer turn, for the admin view.

Each guard (acknowledgement, follow-up, clarification, the candidate's own
question, the turn composer) adds one record when it lets an AI line through,
rejects it, edits it or loses the model call. ``chat_runtime`` opens a fresh
list before the engine runs and puts it on the ``turn_spoken`` event, so the
admin page can show, per turn, which lines the AI wrote, which were replaced
and why (research 24/10, INTERVIEWER_SPEECH_CONTROL_RESEARCH, the guard layer).

A record is ``{"kind", "outcome", "reason", "draft"}``:

* ``kind``: ``ack`` | ``probe`` | ``clarify`` | ``qna`` | ``compose``.
* ``outcome``: ``pass`` (the AI line is said as written), ``reject`` (a draft
  was refused; a later attempt or a scripted line replaces it), ``fix`` (the
  system edited or swapped the line: an extra "nhé", a repeated "À", a second
  question, an acknowledgement that contradicts the next step), ``error`` (the
  model call failed).
* ``draft``: the AI's text before the change (``reject`` and ``fix`` only),
  cut to 200 characters. It is model output, never the candidate's words.

Outside a turn (tests, scripts) nothing is collected and recording is free.
"""

from __future__ import annotations

import contextvars
from typing import Any

KINDS = ("ack", "probe", "clarify", "qna", "compose")
OUTCOMES = ("pass", "reject", "fix", "error")
DRAFT_CHARS = 200
MAX_RECORDS = 20

_records: contextvars.ContextVar[list[dict[str, Any]] | None] = contextvars.ContextVar(
    "speech_checks", default=None
)


def begin() -> list[dict[str, Any]]:
    """Start collecting for one turn; returns the list that fills up."""
    records: list[dict[str, Any]] = []
    _records.set(records)
    return records


def record(kind: str, outcome: str, reason: str | None = None, draft: str | None = None) -> None:
    records = _records.get()
    if records is None or len(records) >= MAX_RECORDS:
        return
    entry: dict[str, Any] = {"kind": kind, "outcome": outcome}
    if reason:
        entry["reason"] = str(reason)[:64]
    if draft and outcome in ("reject", "fix"):
        text = " ".join(str(draft).split())
        entry["draft"] = text if len(text) <= DRAFT_CHARS else text[: DRAFT_CHARS - 1] + "…"
    records.append(entry)


def collected() -> list[dict[str, Any]]:
    return list(_records.get() or [])
