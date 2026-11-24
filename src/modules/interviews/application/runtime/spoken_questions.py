"""Persist Interviewer Agent spoken-question enrichments on frozen turns."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.trace_logging import trace_event
from src.modules.interviews.agent.interviewer.spoken_questions import rewrite_spoken_questions

logger = logging.getLogger(__name__)
GenerateFn = Callable[..., Awaitable[Any]]


async def add_spoken_versions(
    *, db: AsyncSession, session_id: str, locale: str, generate_fn: GenerateFn | None = None
) -> int:
    """Ask the Interviewer Agent to rewrite frozen questions, then persist valid text."""
    if os.getenv("INTERVIEW_SPOKEN_QUESTIONS", "1").strip().lower() in {"0", "false", "off", "no"}:
        return 0
    try:
        result = await db.execute(
            text(
                "SELECT id, question_snapshot FROM interview_turns "
                "WHERE session_id = :sid AND question_version_id IS NOT NULL ORDER BY turn_index"
            ),
            {"sid": session_id},
        )
        rows = [dict(row) for row in result.mappings().all()]
    except Exception as exc:  # Optional wording enrichment must not block opening.
        logger.warning("spoken questions: cannot read turns: %s", exc)
        return 0

    pending: list[dict[str, Any]] = []
    for row in rows:
        snapshot = row.get("question_snapshot") or {}
        if isinstance(snapshot, str):
            try:
                snapshot = json.loads(snapshot)
            except ValueError:
                continue
        question = str(snapshot.get("questionText") or "").strip()
        if not question or snapshot.get("spokenText") or str(snapshot.get("questionType") or "").lower() == "coding":
            continue
        pending.append(
            {
                "id": str(row["id"]),
                "question": question,
                "expected_text": snapshot.get("expectedPoints") or snapshot.get("rubric") or "",
            }
        )
    if not pending:
        return 0

    rewrites = await rewrite_spoken_questions(pending, locale=locale, generate_fn=generate_fn)
    saved = 0
    for item in pending:
        spoken = rewrites.get(item["id"])
        if not spoken:
            continue
        await db.execute(
            text(
                "UPDATE interview_turns SET question_snapshot = question_snapshot || CAST(:extra AS jsonb), "
                "updated_at = now() WHERE id = :id AND session_id = :sid"
            ),
            {"id": item["id"], "sid": session_id, "extra": json.dumps({"spokenText": spoken}, ensure_ascii=False)},
        )
        saved += 1
    if saved:
        await db.commit()
    trace_event(
        "interviewer",
        "spoken_questions_saved",
        session_id=session_id,
        saved=saved,
        rejected=len(pending) - saved,
        total=len(pending),
    )
    return saved
