"""The server-side identity a speculative preview is bound to (V2 spec §7).

One builder, used three times on the same inputs:

1. at **issuance** (``adapters/voice/api/lab.py``), sealed into the preview token;
2. at **redemption** (same module), rebuilt from the database and compared strictly;
3. at the **claim** (``session/chat_runtime.py``), rebuilt once more *under the
   session row lock* and compared again -- the only point at which nothing can
   move between the check and the write.

The third check exists because redemption and the claim are different
transactions: a concurrent message, a replan or the session reaper can change
the turn between the endpoint's validation and the moment the answer is
written. Validating at the endpoint says the preview *was* valid; validating
under the lock says it *is*.

Everything here is pure except the SQL text, which the runtime and the
endpoint share so they read the same rows the same way.
"""

from __future__ import annotations

import json
from hashlib import sha256
from typing import Any

from src.modules.interviews.agents.interviewer.context import build_session_state_from_db
from src.modules.interviews.realtime.anticipation.validator import PreviewKey, preview_key

__all__ = ["TURNS_SQL", "authoritative_preview_key", "only_asked_turn", "stable_hash"]

TURNS_SQL = (
    "SELECT id, turn_index, status, question_version_id, question_snapshot, answer_text, started_at "
    "FROM interview_turns WHERE session_id = :sid ORDER BY turn_index ASC"
)


def only_asked_turn(turns: list[dict[str, Any]]) -> dict[str, Any] | None:
    asked = [turn for turn in turns if turn.get("status") == "ASKED"]
    return asked[0] if len(asked) == 1 else None


def stable_hash(value: Any) -> str:
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")).hexdigest()[:16]


def authoritative_preview_key(
    *,
    session: dict[str, Any],
    transcript: str,
    turns: list[dict[str, Any]] | None = None,
    current_turn: dict[str, Any] | None = None,
    session_state: dict[str, Any] | None = None,
) -> PreviewKey | None:
    """The seven fields of V2 spec §7, read from the server's own state.

    Pass either ``turns`` (the current turn and state are derived here, exactly
    as issuance derives them) or an already-derived ``current_turn`` and
    ``session_state``. Returns ``None`` when the session has no single ASKED
    turn to bind to.

    * turn_id / question_id: the ASKED turn's row id and its question version.
    * state_version: a hash of the session fields a decision depends on --
      stage, which turns were asked and answered, how many probes and
      clarifications this question has used, the session's plan status. If any
      of those moved between preview and commit, the preview was reasoning
      about a different turn.
    * rubric_version: the frozen rubric of this question (from the turn's
      snapshot) with its question version -- a changed rubric is a different
      assessment.
    * policy_version: the frozen interview contract (blueprint payload and
      version id) -- a changed policy is a different interview.
    """
    if current_turn is None:
        if turns is None:
            raise ValueError("authoritative_preview_key needs turns or current_turn")
        current_turn = only_asked_turn(turns)
        if current_turn is None:
            return None
    if session_state is None:
        session_state = build_session_state_from_db(session, list(turns or [current_turn]), current_turn, False)
    snapshot = current_turn.get("question_snapshot") or {}
    contract = session_state.get("interview_contract") or {}
    reference = contract.get("reference") or {}
    return preview_key(
        session_id=session["id"],
        turn_id=current_turn.get("id"),
        question_id=current_turn.get("question_version_id") or current_turn.get("id"),
        transcript=transcript,
        state_version=stable_hash(
            {
                "stage": session_state.get("current_stage"),
                "asked": sorted(map(str, session_state.get("asked_question_ids") or [])),
                "turn_index": current_turn.get("turn_index"),
                "turn_in_question": session_state.get("current_turn_in_question"),
                "clarify_count": session_state.get("clarify_count"),
                "plan_status": session.get("plan_status"),
                "status": session.get("status"),
            }
        ),
        rubric_version=stable_hash(
            {"rubric": snapshot.get("rubric"), "question_version": current_turn.get("question_version_id")}
        ),
        policy_version=stable_hash(
            {"version_id": reference.get("version_id"), "policy": contract.get("snapshot_policy")}
        ),
    )
