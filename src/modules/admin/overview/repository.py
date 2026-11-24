"""Database reads for admin overview and practice reports."""

from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.admin.shared.query_constants import EFFECTIVE_SESSION_MODE

DEMO_MAX_MINUTES = 3


async def load_overview_metrics(db: AsyncSession) -> dict[str, Any]:
    candidates = await db.execute(
        text(
            "SELECT COUNT(*) FROM users u "
            "WHERE EXISTS (SELECT 1 FROM user_role_assignments ura "
            "WHERE ura.user_id = u.id AND ura.role = 'CANDIDATE')"
        )
    )
    sessions = await db.execute(
        text(
            f"""
            SELECT COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE UPPER(COALESCE(s.status, '')) <> 'CLOSED') AS active,
                   COUNT(*) FILTER (WHERE UPPER(COALESCE(s.status, '')) = 'CLOSED') AS closed,
                   COUNT(*) FILTER (
                       WHERE UPPER(COALESCE(s.status, '')) = 'CLOSED'
                         AND EXISTS (SELECT 1 FROM interview_evaluations e WHERE e.session_id = s.id)
                   ) AS graded,
                   COUNT(*) FILTER (WHERE {EFFECTIVE_SESSION_MODE} IN ('text', 'chat')) AS chat_count,
                   COUNT(*) FILTER (WHERE {EFFECTIVE_SESSION_MODE} = 'voice') AS voice_count,
                   COUNT(*) FILTER (WHERE {EFFECTIVE_SESSION_MODE} IN ('call', 'video')) AS video_count,
                   AVG(CASE WHEN s.ended_at IS NOT NULL AND s.started_at IS NOT NULL
                       THEN EXTRACT(EPOCH FROM (s.ended_at - s.started_at)) / 60.0 ELSE NULL END) AS avg_duration
            FROM interview_sessions s
            """
        )
    )
    score = await db.execute(text("SELECT AVG(overall_score) FROM interview_evaluations"))
    trend = await db.execute(
        text(
            "SELECT date_trunc('month', started_at) AS bucket, COUNT(*) AS count "
            "FROM interview_sessions "
            "WHERE started_at >= NOW() - INTERVAL '6 months' "
            "GROUP BY 1 ORDER BY 1 ASC"
        )
    )
    return {
        "total_candidates": candidates.scalar() or 0,
        "sessions": dict(sessions.mappings().first() or {}),
        "average_score": score.scalar(),
        "monthly_trend": [dict(row) for row in trend.mappings().all()],
    }


async def fetch_rows(db: AsyncSession, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    """Read a collection of mappings; missing optional analytics tables return no rows."""
    try:
        return [dict(row) for row in (await db.execute(text(sql), params)).mappings().all()]
    except Exception:
        await db.rollback()
        return []


async def load_practice_data(
    db: AsyncSession, *, since: datetime, include_demo: bool, default_goal: int = 2
) -> tuple[list[dict[str, Any]], dict[str, int], list[dict[str, Any]]]:
    demo_filter = "" if include_demo else f"AND COALESCE(s.duration_minutes, 25) > {DEMO_MAX_MINUTES}"
    sessions_sql = f"""
WITH sess AS (
    SELECT s.id, s.user_id, s.status, s.started_at, s.duration_minutes,
           ev.id AS eval_id, ev.overall_score
    FROM interview_sessions s
    LEFT JOIN LATERAL (
        SELECT e.id, e.overall_score FROM interview_evaluations e
        WHERE e.session_id = s.id ORDER BY e.evaluated_at DESC LIMIT 1
    ) ev ON TRUE
    WHERE s.started_at >= :since {demo_filter}
),
answered AS (
    SELECT te.evaluation_id, COUNT(*) AS n
    FROM interview_turn_evaluations te
    JOIN interview_turns t ON t.id = te.turn_id
    WHERE te.evaluation_id IN (SELECT eval_id FROM sess WHERE eval_id IS NOT NULL)
      AND COALESCE(te.outcome, 'ANSWERED') = 'ANSWERED'
      AND COALESCE(t.question_snapshot->>'stage', '') NOT IN ('', 'WARM_UP', 'CLOSING')
      AND COALESCE(t.question_snapshot->'taxonomyTarget'->>'conceptId', '') <> 'warmup'
    GROUP BY te.evaluation_id
)
SELECT sess.id, sess.user_id, sess.status, sess.started_at, sess.eval_id, sess.overall_score,
       COALESCE(answered.n, 0) AS answered
FROM sess LEFT JOIN answered ON answered.evaluation_id = sess.eval_id
"""
    sessions = await fetch_rows(db, sessions_sql, {"since": since})
    user_ids = sorted({str(row["user_id"]) for row in sessions if row.get("user_id")})
    goals = (
        {
            str(row["id"]): int(row["goal"] or default_goal)
            for row in await fetch_rows(
                db,
                "SELECT id, practice_weekly_goal AS goal FROM users WHERE id = ANY(:ids)",
                {"ids": user_ids},
            )
        }
        if user_ids
        else {}
    )
    reports = await fetch_rows(
        db, "SELECT result FROM real_interview_reports WHERE created_at >= :since", {"since": since}
    )
    return sessions, goals, reports
