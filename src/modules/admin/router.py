from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.security import require_admin
from src.infrastructure.database import get_db

router = APIRouter(prefix="/admin", tags=["admin"])


def _calc_initials(name: str | None, email: str) -> str:
    raw = (name or "").strip()
    if raw:
        parts = raw.split()
        if len(parts) >= 2:
            return (parts[0][0] + parts[-1][0]).upper()
        return raw[:2].upper()
    return email[:2].upper()


@router.get("/overview")
async def get_admin_overview(
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    # 1. Total Candidates
    cand_res = await db.execute(
        text(
            "SELECT COUNT(*) FROM users u "
            "WHERE EXISTS (SELECT 1 FROM user_role_assignments ura "
            "WHERE ura.user_id = u.id AND ura.role = 'CANDIDATE')"
        )
    )
    total_candidates = cand_res.scalar() or 0

    # 2. Total Sessions & Active / Closed
    sess_stats = await db.execute(
        text(
            """
            SELECT 
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE UPPER(status) = 'OPEN') AS active,
                COUNT(*) FILTER (WHERE UPPER(status) = 'CLOSED') AS closed,
                COUNT(*) FILTER (WHERE UPPER(type) = 'CHAT') AS chat_count,
                COUNT(*) FILTER (WHERE UPPER(type) = 'VOICE') AS voice_count,
                COUNT(*) FILTER (WHERE UPPER(type) IN ('CALL', 'VIDEO')) AS video_count,
                AVG(CASE WHEN ended_at IS NOT NULL AND started_at IS NOT NULL 
                    THEN EXTRACT(EPOCH FROM (ended_at - started_at)) / 60.0 ELSE NULL END) AS avg_duration
            FROM interview_sessions
            """
        )
    )
    s_row = sess_stats.mappings().first() or {}
    total_sessions = s_row.get("total") or 0
    active_sessions = s_row.get("active") or 0
    closed_sessions = s_row.get("closed") or 0
    chat_count = s_row.get("chat_count") or 0
    voice_count = s_row.get("voice_count") or 0
    video_count = s_row.get("video_count") or 0
    raw_avg_turnaround = s_row.get("avg_duration")
    avg_turnaround = round(float(raw_avg_turnaround), 1) if raw_avg_turnaround else None

    # 3. Average AI Score: real interview evaluations only (0-10, shown on a
    # 0-100 scale). No evaluations means no average, never an invented one.
    score_res = await db.execute(text("SELECT AVG(overall_score) FROM interview_evaluations"))
    raw_score = score_res.scalar()
    avg_score = round(float(raw_score) * 10, 1) if raw_score is not None else None

    # 4. Sentiment: there is no sentiment signal in the data model. The old value
    # was a constant (92) or derived from the score; report it as unavailable.
    sentiment_ratio = None

    # 5. Monthly Trend (last 6 months distribution)
    trend_res = await db.execute(
        text(
            """
            SELECT 
                TO_CHAR(started_at, 'Mon') as mon,
                EXTRACT(MONTH FROM started_at) as m_num,
                COUNT(*) as count
            FROM interview_sessions
            WHERE started_at >= NOW() - INTERVAL '6 months'
            GROUP BY TO_CHAR(started_at, 'Mon'), EXTRACT(MONTH FROM started_at)
            ORDER BY m_num ASC
            """
        )
    )
    trend_rows = trend_res.mappings().all()
    monthly_trend = [
        {"month": r["mon"], "count": r["count"]}
        for r in trend_rows
    ]

    # 6. Top Cohorts / Tracks: no per-track analytics exist yet. Return none
    # rather than a hardcoded showcase that reads as real data.
    cohorts: list[dict] = []

    return {
        "success": True,
        "overview": {
            "totalCandidates": total_candidates,
            "totalSessions": total_sessions,
            "activeSessions": active_sessions,
            "completedSessions": closed_sessions,
            "averageScore": avg_score,
            "averageTurnaround": avg_turnaround,
            "sentimentScore": sentiment_ratio,
            "monthlyTrend": monthly_trend,
            "modes": {
                "chat": chat_count,
                "voice": voice_count,
                "video": video_count,
            },
            "cohorts": cohorts,
            "aiEngineStatus": "active",
            "updatedAt": datetime.now(timezone.utc).isoformat(),
        },
    }


# Structured sessions store their mode in `mode`; legacy rows only have `type`.
_EFFECTIVE_MODE = "LOWER(COALESCE(s.mode, s.type, 'text'))"
_MODE_FILTERS = {
    "video": f"AND {_EFFECTIVE_MODE} IN ('video', 'call')",
    "chat": f"AND {_EFFECTIVE_MODE} IN ('text', 'chat')",
    "voice": f"AND {_EFFECTIVE_MODE} = 'voice'",
}


def _session_filters(mode: str, search: str | None) -> tuple[str, dict]:
    """WHERE clause shared by the page query, the total and the stats."""
    clause = "WHERE 1=1 " + _MODE_FILTERS.get(mode, "")
    params: dict = {}
    if search and search.strip():
        clause += " AND (LOWER(u.name) LIKE :kw OR LOWER(u.email) LIKE :kw)"
        params["kw"] = f"%{search.strip().lower()}%"
    return clause, params


# Latest evaluation per session (a session can be re-graded).
_LATEST_EVALUATION = """
    LEFT JOIN LATERAL (
        SELECT e.overall_score
        FROM interview_evaluations e
        WHERE e.session_id = s.id
        ORDER BY e.evaluated_at DESC
        LIMIT 1
    ) ev ON TRUE
"""


@router.get("/sessions")
async def list_admin_sessions(
    mode: str = Query("all", description="Mode filter: all, video, chat, voice"),
    search: str | None = Query(None, description="Search candidate name or email"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    where, filter_params = _session_filters(mode, search)
    base = (
        "FROM interview_sessions s LEFT JOIN users u ON s.user_id = u.id "
        "LEFT JOIN job_descriptions j ON j.id = s.job_id " + _LATEST_EVALUATION + where
    )

    rows = (
        await db.execute(
            text(
                f"""
                SELECT s.id, s.status, s.started_at, s.locale, s.language,
                       {_EFFECTIVE_MODE} AS effective_mode,
                       j.title AS job_title,
                       COALESCE(u.name, 'Ứng viên chưa đặt tên') AS candidate_name,
                       COALESCE(u.email, 'unknown@intervia.io') AS candidate_email,
                       ev.overall_score AS ai_score
                {base}
                ORDER BY s.started_at DESC LIMIT :limit OFFSET :offset
                """
            ),
            {**filter_params, "limit": limit, "offset": offset},
        )
    ).mappings().all()

    # Totals and stats cover every matching session, not just this page.
    summary = (
        await db.execute(
            text(
                f"""
                SELECT COUNT(*) AS total,
                       COUNT(*) FILTER (WHERE UPPER(COALESCE(s.status, '')) <> 'CLOSED') AS active,
                       COUNT(*) FILTER (WHERE UPPER(COALESCE(s.status, '')) = 'CLOSED') AS completed,
                       AVG(ev.overall_score) AS avg_score
                {base}
                """
            ),
            filter_params,
        )
    ).mappings().one()

    sessions = []
    for r in rows:
        c_name = r["candidate_name"]
        c_email = r["candidate_email"]
        raw_mode = (r["effective_mode"] or "").lower()
        ui_type = "video" if raw_mode in ("call", "video") else "voice" if raw_mode == "voice" else "chat"

        is_closed = (r["status"] or "").upper() == "CLOSED"
        started_dt = r["started_at"]
        date_label = started_dt.strftime("%d/%m/%Y") if started_dt else ""
        time_label = started_dt.strftime("%H:%M") if started_dt else ""

        # Evaluation score is 0-10; the admin UI shows a percentage. A session
        # that was never evaluated has no score.
        score_val = r["ai_score"]
        final_score = round(float(score_val) * 10) if score_val is not None else None

        sessions.append(
            {
                "id": str(r["id"]),
                "candidateName": c_name,
                "candidateEmail": c_email,
                "candidateInitials": _calc_initials(c_name, c_email),
                # Real job title (null when the JD was deleted); no placeholder roles.
                "position": r["job_title"],
                "teamAndLocation": r["locale"] or r["language"] or "",
                "type": ui_type,
                "aiScore": final_score,
                "dateLabel": date_label,
                "timeLabel": time_label,
                "status": "completed" if is_closed else "scheduled",
                "actionLabel": "admin.interviews.action.viewDetailedReport" if is_closed else "admin.interviews.action.manageBooking",
            }
        )

    avg_score = summary["avg_score"]
    return {
        "success": True,
        "total": int(summary["total"] or 0),
        "stats": {
            "active": int(summary["active"] or 0),
            "completed": int(summary["completed"] or 0),
            # Percentage like aiScore; null when nothing has been graded yet.
            "avgScore": round(float(avg_score) * 10) if avg_score is not None else None,
        },
        "sessions": sessions,
    }
