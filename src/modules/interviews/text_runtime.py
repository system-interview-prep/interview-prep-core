"""P3 structured text interview runtime.

P3 owns deterministic turn lifecycle only. It does not evaluate answers or ask
an LLM to choose/generate questions.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


class TurnStateError(RuntimeError):
    pass


def _turn_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "turnId": row["id"],
        "turnIndex": row["turn_index"],
        "status": row["status"],
        "questionVersionId": str(row["question_version_id"]) if row["question_version_id"] else None,
        "rubricVersionId": str(row["rubric_version_id"]) if row["rubric_version_id"] else None,
        "question": row["question_snapshot"] or {},
        "answerText": row["answer_text"],
        "startedAt": row["started_at"].isoformat() if row["started_at"] else None,
        "completedAt": row["completed_at"].isoformat() if row["completed_at"] else None,
    }


async def _turn(
    db: AsyncSession,
    *,
    session_id: str,
    turn_id: str,
    for_update: bool = False,
) -> dict[str, Any]:
    suffix = " FOR UPDATE" if for_update else ""
    result = await db.execute(
        text(
            """
            SELECT id, session_id, turn_index, status, question_version_id,
                   rubric_version_id, question_snapshot, answer_text,
                   started_at, completed_at
            FROM interview_turns
            WHERE id = :turn_id AND session_id = :session_id
            """
            + suffix
        ),
        {"turn_id": turn_id, "session_id": session_id},
    )
    row = result.mappings().one_or_none()
    if row is None:
        raise ValueError("Interview turn not found")
    return dict(row)


async def read_text_runtime(
    *,
    db: AsyncSession,
    session_row: dict[str, Any],
) -> dict[str, Any]:
    if session_row["mode"] != "text":
        raise TurnStateError("Structured text runtime requires a text session")
    if session_row.get("plan_status") != "LOCKED":
        raise TurnStateError("Interview plan must be LOCKED before text runtime starts")

    result = await db.execute(
        text(
            """
            SELECT id, session_id, turn_index, status, question_version_id,
                   rubric_version_id, question_snapshot, answer_text,
                   started_at, completed_at
            FROM interview_turns
            WHERE session_id = :session_id
            ORDER BY turn_index
            """
        ),
        {"session_id": session_row["id"]},
    )
    rows = [dict(row) for row in result.mappings().all()]
    if not rows:
        raise TurnStateError("Interview has no frozen turns")

    current = next(
        (row for row in rows if row["status"] in {"PLANNED", "ASKED"}),
        None,
    )
    answered = sum(row["status"] in {"ANSWERED", "EVALUATED"} for row in rows)
    return {
        "sessionId": session_row["id"],
        "sessionStatus": session_row["status"],
        "completed": current is None,
        "progress": {
            "answered": answered,
            "total": len(rows),
        },
        "currentTurn": _turn_payload(current) if current else None,
        "turns": [_turn_payload(row) for row in rows],
    }


async def ask_turn(
    *,
    db: AsyncSession,
    session_row: dict[str, Any],
    turn_id: str,
) -> dict[str, Any]:
    if session_row["status"] == "CLOSED":
        raise TurnStateError("Interview session is closed")
    runtime = await read_text_runtime(db=db, session_row=session_row)
    current = runtime["currentTurn"]
    if current is None:
        raise TurnStateError("Interview has no remaining turn")
    if current["turnId"] != turn_id:
        raise TurnStateError("Only the current interview turn can be asked")

    row = await _turn(db, session_id=session_row["id"], turn_id=turn_id, for_update=True)
    if row["status"] == "PLANNED":
        await db.execute(
            text(
                """
                UPDATE interview_turns
                SET status = 'ASKED',
                    started_at = COALESCE(started_at, now()),
                    updated_at = now()
                WHERE id = :turn_id AND session_id = :session_id
                  AND status = 'PLANNED'
                """
            ),
            {"turn_id": turn_id, "session_id": session_row["id"]},
        )
        await db.commit()
        row = await _turn(db, session_id=session_row["id"], turn_id=turn_id)
    elif row["status"] != "ASKED":
        raise TurnStateError(f"Turn cannot be asked from status {row['status']}")
    return _turn_payload(row)


async def answer_turn(
    *,
    db: AsyncSession,
    session_row: dict[str, Any],
    turn_id: str,
    answer_text: str,
) -> dict[str, Any]:
    if session_row["status"] == "CLOSED":
        raise TurnStateError("Interview session is closed")
    cleaned = answer_text.strip()
    if not cleaned:
        raise ValueError("answerText must not be blank")

    row = await _turn(db, session_id=session_row["id"], turn_id=turn_id, for_update=True)
    if row["status"] == "ANSWERED":
        if (row["answer_text"] or "").strip() == cleaned:
            return _turn_payload(row)
        raise TurnStateError("Answered turn is immutable")
    if row["status"] != "ASKED":
        raise TurnStateError("Turn must be ASKED before it can be answered")

    await db.execute(
        text(
            """
            UPDATE interview_turns
            SET status = 'ANSWERED', answer_text = :answer_text,
                completed_at = COALESCE(completed_at, now()), updated_at = now()
            WHERE id = :turn_id AND session_id = :session_id AND status = 'ASKED'
            """
        ),
        {
            "turn_id": turn_id,
            "session_id": session_row["id"],
            "answer_text": cleaned,
        },
    )
    await db.commit()
    return _turn_payload(
        await _turn(db, session_id=session_row["id"], turn_id=turn_id)
    )


async def complete_text_runtime(
    *,
    db: AsyncSession,
    session_row: dict[str, Any],
) -> dict[str, Any]:
    runtime = await read_text_runtime(db=db, session_row=session_row)
    unfinished = [
        turn for turn in runtime["turns"]
        if turn["status"] not in {"ANSWERED", "EVALUATED", "SKIPPED"}
    ]
    if unfinished:
        raise TurnStateError("Interview cannot complete while turns remain unanswered")

    await db.execute(
        text(
            """
            UPDATE interview_sessions
            SET status = 'CLOSED', ended_at = COALESCE(ended_at, now()), updated_at = now()
            WHERE id = :session_id AND status <> 'CLOSED'
            """
        ),
        {"session_id": session_row["id"]},
    )
    await db.commit()
    return {
        **runtime,
        "sessionStatus": "CLOSED",
        "completed": True,
    }
