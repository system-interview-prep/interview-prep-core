"""Database access for interview progress queries and weekly goals."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.interviews.application.progress_calibration import load_calibration
from src.modules.interviews.application.progress_sessions import build_progress
from src.modules.interviews.application.progress_skills import (
    _TIER_RANK,
    DEFAULT_WEEKLY_GOAL,
    MAX_SESSIONS,
    WEEKLY_GOAL_RANGE,
    SessionRow,
    TurnRow,
)


def _json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


async def weekly_goal_for(db: AsyncSession, user_id: str) -> int:
    try:
        value = await db.scalar(
            text("SELECT practice_weekly_goal FROM users WHERE id = :uid"), {"uid": user_id}
        )
    except Exception:  # noqa: BLE001 - migration 20261019_0028 not applied yet
        await db.rollback()
        return DEFAULT_WEEKLY_GOAL
    try:
        goal = int(value)
    except (TypeError, ValueError):
        return DEFAULT_WEEKLY_GOAL
    low, high = WEEKLY_GOAL_RANGE
    return goal if low <= goal <= high else DEFAULT_WEEKLY_GOAL


async def set_weekly_goal(db: AsyncSession, user_id: str, goal: int) -> int:
    low, high = WEEKLY_GOAL_RANGE
    if not low <= goal <= high:
        raise ValueError(f"weeklyGoal must be between {low} and {high}")
    await db.execute(
        text("UPDATE users SET practice_weekly_goal = :goal WHERE id = :uid"), {"goal": goal, "uid": user_id}
    )
    await db.commit()
    return goal


async def load_progress(
    db: AsyncSession, user_id: str, *, now: datetime | None = None, tz_offset_minutes: int = 0
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    session_rows = (
        (
            await db.execute(
                text(
                    """
                    SELECT s.id, s.job_id, j.title AS job_title, s.started_at, s.mode, s.end_reason,
                       s.resume_id, c.filename AS resume_name,
                       p.source_context->>'practiceTier' AS tier,
                       p.plan_payload->'difficulty'->>'level' AS difficulty,
                       ev.id AS evaluation_id, ev.overall_score, ev.grader_version
                FROM interview_sessions s
                JOIN interview_session_plans p ON p.session_id = s.id
                LEFT JOIN job_descriptions j ON j.id = s.job_id
                LEFT JOIN user_cvs c ON c.id = s.resume_id
                LEFT JOIN LATERAL (
                    SELECT e.id, e.overall_score, e.grader_version
                    FROM interview_evaluations e
                    WHERE e.session_id = s.id
                    ORDER BY e.evaluated_at DESC
                    LIMIT 1
                ) ev ON TRUE
                WHERE s.user_id = :uid
                ORDER BY s.started_at DESC
                LIMIT :max_sessions
                """
                ),
                {"uid": user_id, "max_sessions": MAX_SESSIONS},
            )
        )
        .mappings()
        .all()
    )
    # Every started session counts toward the weekly streak; only graded ones are plotted.
    all_starts = [row["started_at"] for row in session_rows if row["started_at"]]
    evaluated = [row for row in session_rows if row["evaluation_id"] and row["started_at"]]
    sessions = [
        SessionRow(
            session_id=str(row["id"]),
            job_id=str(row["job_id"]) if row["job_id"] else None,
            job_title=row["job_title"],
            started_at=row["started_at"],
            overall_score=float(row["overall_score"]) if row["overall_score"] is not None else None,
            tier=row["tier"] if row["tier"] in _TIER_RANK else "standard",
            difficulty=row["difficulty"],
            resume_id=str(row["resume_id"]) if row["resume_id"] else None,
            resume_name=row["resume_name"],
            grader_version=row.get("grader_version"),
            mode=str(row.get("mode") or "text"),
            end_reason=row.get("end_reason"),
        )
        for row in evaluated
    ]
    turns: list[TurnRow] = []
    if evaluated:
        turn_rows = (
            (
                await db.execute(
                    text(
                        """
                    SELECT t.session_id, t.turn_index, t.question_snapshot, te.score, te.outcome,
                           te.rubric_points, te.star_analysis,
                           EXISTS (
                               SELECT 1 FROM interview_chat_messages m
                               WHERE m.turn_id = t.id AND m.message_type = 'PROBE'
                                 AND COALESCE(m.metadata->>'probeKind', 'gap') = 'gap'
                           ) AS needed_probe
                    FROM interview_turn_evaluations te
                    JOIN interview_turns t ON t.id = te.turn_id
                    WHERE te.evaluation_id = ANY(:eval_ids)
                    """
                    ),
                    {"eval_ids": [row["evaluation_id"] for row in evaluated]},
                )
            )
            .mappings()
            .all()
        )
        for row in turn_rows:
            snap = _json(row["question_snapshot"]) or {}
            target = snap.get("taxonomyTarget") or {}
            turns.append(
                TurnRow(
                    session_id=str(row["session_id"]),
                    stage=str(snap.get("stage") or ""),
                    concept_id=target.get("conceptId"),
                    label=target.get("label"),
                    difficulty=snap.get("difficulty"),
                    score=float(row["score"]) if row["score"] is not None else None,
                    outcome=str(row["outcome"] or "ANSWERED"),
                    needed_probe=bool(row["needed_probe"]),
                    order=int(row["turn_index"] or 0),
                    rubric_points=[p for p in (_json(row.get("rubric_points")) or []) if isinstance(p, dict)],
                    star=_json(row.get("star_analysis")) or {},
                    question=str(snap.get("questionText") or snap.get("question_text") or ""),
                )
            )
    return build_progress(
        sessions,
        turns,
        now=now,
        tz_offset_minutes=tz_offset_minutes,
        weekly_goal=await weekly_goal_for(db, user_id),
        all_session_starts=all_starts,
        calibration=await load_calibration(db),
    )
