"""Database access for admin interview and access-log views."""

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.admin.shared.query_constants import EFFECTIVE_SESSION_MODE

LATEST_EVALUATION = """
LEFT JOIN LATERAL (
    SELECT e.overall_score FROM interview_evaluations e
    WHERE e.session_id = s.id ORDER BY e.evaluated_at DESC LIMIT 1
) ev ON TRUE
"""
READ_ACTIONS = ("OBSERVE_INTERVIEW_ROOM", "VIEW_CANDIDATE_ANSWER", "VIEW_USER_ACTIVITY", "VIEW_AI_TRACE")
ANSWER_PREVIEW_CHARS = 1500

_MODE_FILTERS = {
    "video": f"AND {EFFECTIVE_SESSION_MODE} IN ('video', 'call')",
    "chat": f"AND {EFFECTIVE_SESSION_MODE} IN ('text', 'chat')",
    "voice": f"AND {EFFECTIVE_SESSION_MODE} = 'voice'",
}
_REPORT_FILTERS = {
    "graded": "AND ev.overall_score IS NOT NULL",
    "missing": "AND UPPER(COALESCE(s.status, '')) = 'CLOSED' AND ev.overall_score IS NULL",
    "open": "AND UPPER(COALESCE(s.status, '')) <> 'CLOSED'",
}


def access_log_filter(kind: str) -> tuple[str, dict[str, Any]]:
    if kind == "reads":
        return "WHERE l.action = ANY(:actions)", {"actions": list(READ_ACTIONS)}
    if kind == "changes":
        return "WHERE NOT (l.action = ANY(:actions))", {"actions": list(READ_ACTIONS)}
    return "", {}


def _session_filters(mode: str, search: str | None, report: str) -> tuple[str, dict[str, Any]]:
    clause = "WHERE 1=1 " + _MODE_FILTERS.get(mode, "") + " " + _REPORT_FILTERS.get(report, "")
    params: dict[str, Any] = {}
    if search and search.strip():
        clause += " AND (LOWER(u.name) LIKE :kw OR LOWER(u.email) LIKE :kw)"
        params["kw"] = f"%{search.strip().lower()}%"
    return clause, params


async def list_sessions(
    db: AsyncSession, *, mode: str, search: str | None, report: str, limit: int, offset: int
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    where, params = _session_filters(mode, search, report)
    base = (
        "FROM interview_sessions s LEFT JOIN users u ON s.user_id = u.id "
        "LEFT JOIN job_descriptions j ON j.id = s.job_id " + LATEST_EVALUATION + where
    )
    rows = (
        await db.execute(
            text(
                f"""
                SELECT s.id, s.status, s.started_at, s.locale, s.language, s.user_id, s.end_reason,
                       {EFFECTIVE_SESSION_MODE} AS effective_mode, j.title AS job_title,
                       COALESCE(u.name, 'Ứng viên chưa đặt tên') AS candidate_name,
                       COALESCE(u.email, 'unknown@intervia.io') AS candidate_email,
                       ev.overall_score AS ai_score
                {base}
                ORDER BY s.started_at DESC LIMIT :limit OFFSET :offset
                """
            ),
            {**params, "limit": limit, "offset": offset},
        )
    ).mappings().all()
    summary = (
        await db.execute(
            text(
                f"""
                SELECT COUNT(*) AS total,
                       COUNT(*) FILTER (WHERE UPPER(COALESCE(s.status, '')) <> 'CLOSED') AS active,
                       COUNT(*) FILTER (WHERE UPPER(COALESCE(s.status, '')) = 'CLOSED') AS completed,
                       COUNT(*) FILTER (
                           WHERE UPPER(COALESCE(s.status, '')) = 'CLOSED' AND ev.overall_score IS NULL
                       ) AS ungraded,
                       AVG(ev.overall_score) AS avg_score
                {base}
                """
            ),
            params,
        )
    ).mappings().one()
    return [dict(row) for row in rows], dict(summary)


async def observer_session(db: AsyncSession, session_id: str) -> dict[str, Any] | None:
    row = (
        await db.execute(
            text("SELECT id, mode, status FROM interview_sessions WHERE CAST(id AS TEXT) = :sid"),
            {"sid": session_id},
        )
    ).mappings().one_or_none()
    return dict(row) if row else None


async def candidate_answers(db: AsyncSession, session_id: str, turn_id: str) -> list[dict[str, Any]]:
    rows = (
        await db.execute(
            text(
                "SELECT m.message_type, m.content, m.created_at FROM interview_chat_messages m "
                "JOIN interview_turns t ON t.id = m.turn_id "
                "WHERE m.session_id = :sid AND t.session_id = :sid AND CAST(m.turn_id AS TEXT) = :tid "
                "AND m.role = 'user' ORDER BY m.sequence"
            ),
            {"sid": session_id, "tid": turn_id},
        )
    ).mappings().all()
    return [dict(row) for row in rows]


async def access_logs(
    db: AsyncSession, *, where: str, params: dict[str, Any], limit: int, offset: int
) -> tuple[list[dict[str, Any]], int]:
    rows = (
        await db.execute(
            text(
                "SELECT l.id, l.admin_email, l.action, l.session_id, l.target_id, l.detail, l.created_at, "
                "u.name AS candidate_name, u.email AS candidate_email "
                "FROM admin_access_logs l "
                "LEFT JOIN interview_sessions s ON s.id = l.session_id "
                "LEFT JOIN users u ON u.id = COALESCE(s.user_id, "
                "CASE WHEN l.action = 'VIEW_USER_ACTIVITY' THEN l.target_id END) "
                f"{where} ORDER BY l.created_at DESC, l.id DESC LIMIT :limit OFFSET :offset"
            ),
            {**params, "limit": limit, "offset": offset},
        )
    ).mappings().all()
    total = int((await db.execute(text(f"SELECT COUNT(*) FROM admin_access_logs l {where}"), params)).scalar() or 0)
    return [dict(row) for row in rows], total


async def log_candidate_read(
    db: AsyncSession, *, admin: dict, action: str, session_id: str, target_id: str, detail: dict[str, Any]
) -> None:
    await db.execute(
        text(
            "INSERT INTO admin_access_logs "
            "(admin_user_id, admin_email, action, session_id, target_id, detail) "
            "VALUES (:uid, :email, :action, :sid, :target, CAST(:detail AS JSONB))"
        ),
        {
            "uid": admin["sub"], "email": admin.get("email"), "action": action,
            "sid": session_id, "target": target_id, "detail": json.dumps(detail),
        },
    )
    await db.commit()
