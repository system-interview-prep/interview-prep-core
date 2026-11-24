"""SQL repositories for admin system and account activity views."""

import time
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

CV_LIMIT = 20
SESSION_LIMIT = 30
MATCHING_LIMIT = 10
REPORT_LIMIT = 10


async def _rows(db: AsyncSession, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        return [dict(row) for row in (await db.execute(text(sql), params)).mappings().all()]
    except Exception:
        await db.rollback()
        return []


async def database_snapshot(db: AsyncSession) -> tuple[int, str | None]:
    started = time.monotonic()
    await db.execute(text("SELECT 1"))
    latency_ms = round((time.monotonic() - started) * 1000)
    try:
        applied = await db.scalar(text("SELECT version_num FROM alembic_version LIMIT 1"))
    except Exception:
        await db.rollback()
        applied = None
    return latency_ms, applied


async def latest_voice_event(db: AsyncSession) -> Any | None:
    return await db.scalar(
        text(
            "SELECT MAX(created_at) FROM interview_pipeline_events "
            "WHERE event IN ('voice_tts_metrics', 'voice_state', 'voice_session_started')"
        )
    )


async def load_user_activity(db: AsyncSession, user_id: str) -> dict[str, Any] | None:
    params = {"uid": user_id}
    users = await _rows(
        db,
        "SELECT u.id, u.email, u.name, u.provider, u.is_active, u.created_at, u.practice_weekly_goal, "
        "COALESCE(ARRAY_AGG(ura.role ORDER BY ura.role) FILTER (WHERE ura.role IS NOT NULL), '{}') AS roles "
        "FROM users u LEFT JOIN user_role_assignments ura ON ura.user_id = u.id "
        "WHERE u.id = :uid GROUP BY u.id",
        params,
    )
    if not users:
        return None
    cvs = await _rows(
        db,
        "SELECT id, filename, status, error, created_at, updated_at FROM user_cvs "
        "WHERE user_id = :uid ORDER BY created_at DESC LIMIT :limit",
        {**params, "limit": CV_LIMIT},
    )
    sessions = await _rows(
        db,
        "SELECT s.id, s.status, LOWER(COALESCE(s.mode, s.type, 'text')) AS mode, s.started_at, s.ended_at, "
        "s.end_reason, s.duration_minutes, j.title AS job_title, "
        "(SELECT e.overall_score FROM interview_evaluations e WHERE e.session_id = s.id "
        "ORDER BY e.evaluated_at DESC LIMIT 1) AS score "
        "FROM interview_sessions s LEFT JOIN job_descriptions j ON j.id = s.job_id "
        "WHERE s.user_id = :uid ORDER BY s.started_at DESC NULLS LAST LIMIT :limit",
        {**params, "limit": SESSION_LIMIT},
    )
    session_totals = await _rows(
        db,
        "SELECT COUNT(*) AS n, "
        "COUNT(*) FILTER (WHERE UPPER(COALESCE(s.status, '')) = 'CLOSED') AS closed, "
        "COUNT(ev.score) AS graded, "
        "COUNT(*) FILTER (WHERE UPPER(COALESCE(s.status, '')) = 'CLOSED' AND ev.score IS NULL) AS ungraded, "
        "AVG(ev.score) AS avg_score, MAX(ev.score) AS best_score "
        "FROM interview_sessions s LEFT JOIN LATERAL ("
        "SELECT e.overall_score AS score FROM interview_evaluations e WHERE e.session_id = s.id "
        "ORDER BY e.evaluated_at DESC LIMIT 1) ev ON TRUE WHERE s.user_id = :uid",
        params,
    )
    matching = await _rows(
        db,
        "SELECT m.run_id, MIN(m.created_at) AS started_at, "
        "(ARRAY_AGG(m.job_id) FILTER (WHERE m.job_id IS NOT NULL))[1] AS job_id, "
        "BOOL_OR(m.status = 'error') AS has_error, BOOL_OR(m.stage = 'done') AS done "
        "FROM matching_pipeline_events m WHERE m.user_id = :uid "
        "GROUP BY m.run_id ORDER BY started_at DESC LIMIT :limit",
        {**params, "limit": MATCHING_LIMIT},
    )
    job_ids = sorted({str(row["job_id"]) for row in matching if row.get("job_id")})
    titles = (
        {
            str(row["id"]): row["title"]
            for row in await _rows(
                db, "SELECT id, title FROM job_descriptions WHERE id = ANY(:ids)", {"ids": job_ids}
            )
        }
        if job_ids
        else {}
    )
    reports = await _rows(
        db,
        "SELECT id, company, interview_date, stage, result FROM real_interview_reports "
        "WHERE user_id = :uid ORDER BY interview_date DESC LIMIT :limit",
        {**params, "limit": REPORT_LIMIT},
    )
    return {
        "user": users[0], "cvs": cvs, "sessions": sessions,
        "session_totals": session_totals, "matching": matching, "titles": titles, "reports": reports,
    }
