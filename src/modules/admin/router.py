import asyncio
import json
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.admin_audit import record_admin_action
from src.core.security import require_admin
from src.infrastructure.database import get_db
from src.modules.admin import (
    ai_overview,
    cv_parsing_runs,
    langfuse_view,
    matching_runs,
    practice_stats,
    room_observer,
    system_config,
    system_health,
    user_activity,
)
from src.modules.interviews.facade import (
    InterviewAdminActionError,
    admin_batch_grading_state,
    admin_close_interview_session,
    admin_count_gradable_sessions,
    admin_grade_interview_session,
    admin_grade_pending_sessions,
    ai_call_view,
    interview_ai_alerts,
    interview_ai_health,
    interview_langfuse_links,
    interview_question_quality_report,
    interview_speech_control,
    interviewer_playbook,
    live_interview_sessions,
    review_interview_speech_check,
    session_pipeline,
    session_plan_audit,
    summarize_ai_calls,
)
from src.modules.user_cvs.facade import CvAdminReparseError, reparse_cv_as_admin

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
            f"""
            SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE UPPER(COALESCE(s.status, '')) <> 'CLOSED') AS active,
                COUNT(*) FILTER (WHERE UPPER(COALESCE(s.status, '')) = 'CLOSED') AS closed,
                COUNT(*) FILTER (
                    WHERE UPPER(COALESCE(s.status, '')) = 'CLOSED'
                      AND EXISTS (SELECT 1 FROM interview_evaluations e WHERE e.session_id = s.id)
                ) AS graded,
                COUNT(*) FILTER (WHERE {_EFFECTIVE_MODE} IN ('text', 'chat')) AS chat_count,
                COUNT(*) FILTER (WHERE {_EFFECTIVE_MODE} = 'voice') AS voice_count,
                COUNT(*) FILTER (WHERE {_EFFECTIVE_MODE} IN ('call', 'video')) AS video_count,
                AVG(CASE WHEN s.ended_at IS NOT NULL AND s.started_at IS NOT NULL
                    THEN EXTRACT(EPOCH FROM (s.ended_at - s.started_at)) / 60.0 ELSE NULL END) AS avg_duration
            FROM interview_sessions s
            """
        )
    )
    s_row = sess_stats.mappings().first() or {}
    total_sessions = s_row.get("total") or 0
    active_sessions = s_row.get("active") or 0
    closed_sessions = s_row.get("closed") or 0
    graded_sessions = s_row.get("graded") or 0
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
            # Closed sessions with a report; the rest ended (finished, left, timed out) without one.
            "gradedSessions": graded_sessions,
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


_REPORT_FILTERS = {
    "graded": "AND ev.overall_score IS NOT NULL",
    "missing": "AND UPPER(COALESCE(s.status, '')) = 'CLOSED' AND ev.overall_score IS NULL",
    "open": "AND UPPER(COALESCE(s.status, '')) <> 'CLOSED'",
}


def _session_filters(mode: str, search: str | None, report: str = "all") -> tuple[str, dict]:
    """WHERE clause shared by the page query, the total and the stats."""
    clause = "WHERE 1=1 " + _MODE_FILTERS.get(mode, "") + " " + _REPORT_FILTERS.get(report, "")
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
    report: str = Query("all", description="Report filter: all, graded, missing, open"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    where, filter_params = _session_filters(mode, search, report)
    base = (
        "FROM interview_sessions s LEFT JOIN users u ON s.user_id = u.id "
        "LEFT JOIN job_descriptions j ON j.id = s.job_id " + _LATEST_EVALUATION + where
    )

    rows = (
        await db.execute(
            text(
                f"""
                SELECT s.id, s.status, s.started_at, s.locale, s.language, s.user_id, s.end_reason,
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
                       COUNT(*) FILTER (
                           WHERE UPPER(COALESCE(s.status, '')) = 'CLOSED' AND ev.overall_score IS NULL
                       ) AS ungraded,
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
                # completed: closed with a report; action_needed: closed without one.
                "status": ("completed" if score_val is not None else "action_needed")
                if is_closed
                else "scheduled",
                "userId": str(r["user_id"]) if r.get("user_id") else None,
                "endReason": r.get("end_reason"),
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
            "ungraded": int(summary.get("ungraded") or 0),
            # Percentage like aiScore; null when nothing has been graded yet.
            "avgScore": round(float(avg_score) * 10) if avg_score is not None else None,
        },
        "sessions": sessions,
    }


@router.get("/sessions/live")
async def get_admin_live_sessions(
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    group: Literal[
        "all", "attention", "interviewing", "preparing", "grading", "finished"
    ] = Query(default="all"),
    q: str | None = Query(default=None, max_length=120),
    limit: int = Query(default=30, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict:
    """Interviews running now (in progress, being prepared or graded), with their step.

    ``counts`` covers every live session; ``sessions`` is one filtered page of them.
    """
    return {
        "success": True,
        **await live_interview_sessions(db, group=group, q=q, limit=limit, offset=offset),
    }


@router.post("/sessions/{session_id}/observe")
async def observe_interview_room(
    session_id: str,
    admin: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """A hidden, listen-only LiveKit seat in a running voice/video interview; every seat is logged."""
    from src.core.config import get_settings

    row = (
        await db.execute(
            text("SELECT id, mode, status FROM interview_sessions WHERE CAST(id AS TEXT) = :sid"),
            {"sid": session_id},
        )
    ).mappings().one_or_none()
    refusal = room_observer.observe_refusal(dict(row) if row else None)
    if refusal is not None:
        raise HTTPException(status_code=refusal[0], detail=refusal[1])
    settings = get_settings()
    if not settings.livekit_url or not settings.livekit_api_key or not settings.livekit_api_secret:
        raise HTTPException(status_code=503, detail="livekit_not_configured")
    try:
        from livekit import api
    except ImportError as exc:
        raise HTTPException(status_code=503, detail="livekit_not_installed") from exc
    running = await room_observer.room_is_running(
        api,
        url=settings.livekit_url,
        key=settings.livekit_api_key,
        secret=settings.livekit_api_secret,
        session_id=session_id,
    )
    if running is None:
        raise HTTPException(status_code=503, detail="livekit_unreachable")
    if not running:
        raise HTTPException(status_code=409, detail="room_not_started")
    try:
        await db.execute(
            text(
                "INSERT INTO admin_access_logs "
                "(admin_user_id, admin_email, action, session_id, target_id, detail) "
                "VALUES (:uid, :email, 'OBSERVE_INTERVIEW_ROOM', :sid, :room, CAST(:detail AS JSONB))"
            ),
            {
                "uid": admin["sub"],
                "email": admin.get("email"),
                "sid": session_id,
                "room": room_observer.room_name(session_id),
                "detail": json.dumps({"mode": row["mode"]}),
            },
        )
        await db.commit()
    except Exception as exc:
        await db.rollback()
        raise HTTPException(status_code=503, detail="Access log is not migrated (0030)") from exc
    token = room_observer.observer_token(
        api,
        key=settings.livekit_api_key,
        secret=settings.livekit_api_secret,
        session_id=session_id,
        admin_id=str(admin["sub"]),
        admin_name=admin.get("name") or admin.get("email"),
    )
    return {
        "success": True,
        "serverUrl": settings.livekit_url,
        "participantToken": token,
        "roomName": room_observer.room_name(session_id),
        "mode": row["mode"],
        "logged": True,
    }


# A candidate's own words, shown only on request and logged (admin_access_logs).
_ANSWER_PREVIEW_CHARS = 1500


@router.get("/sessions/{session_id}/turns/{turn_id}/answers")
async def get_admin_turn_answers(
    session_id: str,
    turn_id: str,
    admin: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """The candidate's answers to one question, shortened; every read is logged.

    Refuses (503) when the access log cannot be written: candidate data is never
    shown without an audit record.
    """
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
    try:
        await db.execute(
            text(
                "INSERT INTO admin_access_logs "
                "(admin_user_id, admin_email, action, session_id, target_id, detail) "
                "VALUES (:uid, :email, 'VIEW_CANDIDATE_ANSWER', :sid, :tid, CAST(:detail AS JSONB))"
            ),
            {
                "uid": admin["sub"],
                "email": admin.get("email"),
                "sid": session_id,
                "tid": turn_id,
                "detail": json.dumps({"answers": len(rows)}),
            },
        )
        await db.commit()
    except Exception as exc:
        await db.rollback()
        raise HTTPException(status_code=503, detail="Access log is not migrated (0030)") from exc
    answers = []
    for row in rows:
        content = str(row["content"] or "")
        answers.append(
            {
                "type": row["message_type"],
                "at": row["created_at"].isoformat() if row["created_at"] else None,
                "text": content[:_ANSWER_PREVIEW_CHARS],
                "truncated": len(content) > _ANSWER_PREVIEW_CHARS,
                "chars": len(content),
            }
        )
    return {"success": True, "answers": answers, "logged": True}


# Reads of candidate data; every other action is a change.
READ_ACTIONS = ("OBSERVE_INTERVIEW_ROOM", "VIEW_CANDIDATE_ANSWER", "VIEW_USER_ACTIVITY", "VIEW_AI_TRACE")


def _access_log_filter(kind: str) -> tuple[str, dict]:
    if kind == "reads":
        return "WHERE l.action = ANY(:actions)", {"actions": list(READ_ACTIONS)}
    if kind == "changes":
        return "WHERE NOT (l.action = ANY(:actions))", {"actions": list(READ_ACTIONS)}
    return "", {}


@router.get("/access-logs")
async def list_admin_access_logs(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    kind: Literal["all", "reads", "changes"] = Query("all"),
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Who read candidate data and who changed what, newest first."""
    where, params = _access_log_filter(kind)
    try:
        rows = (
            await db.execute(
                text(
                    "SELECT l.id, l.admin_email, l.action, l.session_id, l.target_id, l.detail, "
                    "l.created_at, "
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
        count_sql = text(f"SELECT COUNT(*) FROM admin_access_logs l {where}")
        total = int((await db.execute(count_sql, params)).scalar() or 0)
    except Exception as exc:
        await db.rollback()
        raise HTTPException(status_code=503, detail="Access log is not migrated (0030)") from exc
    return {
        "success": True,
        "total": total,
        "logs": [
            {
                "id": row["id"],
                "adminEmail": row["admin_email"],
                "action": row["action"],
                "sessionId": row["session_id"],
                "targetId": row["target_id"],
                "detail": row["detail"] or {},
                "candidate": row["candidate_name"] or row["candidate_email"],
                "at": row["created_at"].isoformat() if row["created_at"] else None,
            }
            for row in rows
        ],
    }


@router.get("/sessions/{session_id}/plan")
async def get_admin_session_plan(
    session_id: str,
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """A session as planned and as asked: budget, difficulty, sources, follow-ups."""
    audit = await session_plan_audit(db, session_id)
    if audit is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"success": True, **audit}


@router.get("/sessions/{session_id}/pipeline")
async def get_admin_session_pipeline(
    session_id: str,
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """What the AI did for one interview, stage by stage (polled while it runs)."""
    pipeline = await session_pipeline(db, session_id)
    if pipeline is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"success": True, **pipeline}


@router.get("/ai/alerts")
async def get_admin_ai_alerts(
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Problems to act on now: failed or stuck grading, live errors, high fallback rates."""
    return {"success": True, **await interview_ai_alerts(db)}


@router.get("/ai/health")
async def get_admin_ai_health(
    days: int = Query(7, ge=1, le=90),
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Fallback rates, latencies, failures and stuck sessions over a window."""
    try:
        health = await interview_ai_health(db, days=days)
    except Exception as exc:  # tables missing until migration 0029 is applied
        await db.rollback()
        raise HTTPException(status_code=503, detail="AI observability tables are not migrated") from exc
    return {"success": True, **health, "langfuse": await interview_langfuse_links()}


@router.get("/ai/models")
async def get_admin_ai_models(
    days: int = Query(30, ge=1, le=90),
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Provider and model of each AI feature: configured, and seen in ai_call_logs."""
    return {"success": True, **await ai_overview.models_view(db, days=days)}


# --------------------------------------------------------------------------- Langfuse in the console


@router.get("/langfuse/traces/{trace_id}")
async def get_langfuse_trace(
    trace_id: str,
    session_id: str | None = Query(None, alias="sessionId", max_length=36),
    admin: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """One turn's model calls and scores, read back from Langfuse."""
    view = await langfuse_view.trace_view(trace_id[:64])
    # Sampled turns carry the candidate's (masked) answer: reading it is logged
    # like "Xem câu trả lời".
    if view.get("connected") and any(o.get("input") for o in view.get("observations") or []):
        await _log_admin_action(db, admin, "VIEW_AI_TRACE", session_id=session_id, target_id=trace_id[:64])
    return {"success": True, **view}


class TraceReviewRequest(BaseModel):
    name: str = Field(max_length=64)
    value: bool
    comment: str | None = Field(default=None, max_length=500)
    sessionId: str | None = Field(default=None, max_length=36)


@router.post("/langfuse/traces/{trace_id}/reviews", status_code=201)
async def review_langfuse_trace(
    trace_id: str,
    payload: TraceReviewRequest,
    admin: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """A reviewer's verdict on a turn, written to Langfuse as a ``human.*`` score."""
    try:
        await langfuse_view.record_review(trace_id[:64], payload.name, payload.value, payload.comment)
    except langfuse_view.ReviewScoreError as exc:
        raise HTTPException(status_code=422 if str(exc) == "unknown_score" else 503, detail=str(exc)) from exc
    await _log_admin_action(
        db,
        admin,
        "REVIEW_AI_TURN",
        session_id=payload.sessionId,
        target_id=trace_id[:64],
        detail={"score": payload.name, "value": payload.value, "note": payload.comment},
    )
    return {"success": True}


@router.get("/sessions/{session_id}/langfuse")
async def get_session_langfuse(session_id: str, _: dict = Depends(require_admin)) -> dict:
    """Every Langfuse trace of one interview (turns and grading) with totals and session scores."""
    return {"success": True, **await langfuse_view.session_view(session_id[:36])}


@router.get("/langfuse/flagged")
async def get_flagged_turns(
    days: int = Query(7, ge=1, le=90),
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """The latest turns a rule flagged, with the interview and turn they belong to."""
    view = await langfuse_view.flagged_turns(days=days)
    items = view.get("items") or []
    if items:
        rows = (
            await db.execute(
                text(
                    "SELECT session_id, turn_index, payload->>'langfuse_trace_id' AS trace_id, "
                    "payload->'flags' AS flags FROM interview_pipeline_events "
                    "WHERE event = 'turn_spoken' AND payload->>'langfuse_trace_id' = ANY(:ids)"
                ),
                {"ids": [item["traceId"] for item in items]},
            )
        ).mappings().all()
        by_trace = {row["trace_id"]: row for row in rows}
        for item in items:
            row = by_trace.get(item["traceId"])
            item["sessionId"] = row["session_id"] if row else None
            item["turnIndex"] = row["turn_index"] if row else None
            item["flags"] = list(row["flags"] or []) if row else []
    return {"success": True, **view}


# --------------------------------------------------------------------------- interviewer playbook


class PlaybookReflectRequest(BaseModel):
    days: int = Field(default=14, ge=1, le=90)


class PlaybookEntryRequest(BaseModel):
    step: Literal["all", "evaluate_answer", "probe", "clarify"] = "all"
    text: str = Field(min_length=5, max_length=400)
    skill: str | None = Field(default=None, max_length=160)


class PlaybookDecisionRequest(BaseModel):
    decision: Literal["APPROVE", "REJECT", "RETIRE"]
    note: str | None = Field(default=None, max_length=500)
    text: str | None = Field(default=None, max_length=400)


def _playbook_error(exc: Exception) -> HTTPException:
    return HTTPException(status_code=getattr(exc, "status_code", 400), detail=getattr(exc, "code", str(exc)))


@router.get("/interviewer-playbook")
async def get_interviewer_playbook(
    _: dict = Depends(require_admin), db: AsyncSession = Depends(get_db)
) -> dict:
    """Lessons from past interviews: proposed, active (in the prompts), retired; the last reflection."""
    return {"success": True, **await interviewer_playbook.view(db)}


@router.post("/interviewer-playbook/reflect", status_code=202)
async def reflect_interviewer_playbook(
    payload: PlaybookReflectRequest | None = None,
    admin: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Propose lessons from recent flagged and reviewed turns (one LLM call, background)."""
    days = payload.days if payload else 14
    try:
        run = interviewer_playbook.start_reflection(days=days)
    except interviewer_playbook.PlaybookError as exc:
        raise _playbook_error(exc) from exc
    await _log_admin_action(db, admin, "REFLECT_PLAYBOOK", detail={"days": days})
    return {"success": True, "run": run}


@router.post("/interviewer-playbook", status_code=201)
async def add_playbook_entry(
    payload: PlaybookEntryRequest, admin: dict = Depends(require_admin), db: AsyncSession = Depends(get_db)
) -> dict:
    """A lesson written by hand; it is PROPOSED like the reflector's and needs approval."""
    try:
        entry_id = await interviewer_playbook.add_manual(
            db,
            step=payload.step,
            text_value=payload.text,
            skill=payload.skill,
            actor_id=str(admin.get("sub") or ""),
        )
    except interviewer_playbook.PlaybookError as exc:
        raise _playbook_error(exc) from exc
    await _log_admin_action(
        db, admin, "ADD_PLAYBOOK_ENTRY", target_id=entry_id, detail={"step": payload.step}
    )
    return {"success": True, "id": entry_id}


@router.post("/interviewer-playbook/{entry_id}/decision")
async def decide_playbook_entry(
    entry_id: str,
    payload: PlaybookDecisionRequest,
    admin: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Approve (into the prompts), reject a proposal, or retire an active lesson."""
    try:
        result = await interviewer_playbook.decide(
            db,
            entry_id[:36],
            decision=payload.decision,
            actor_id=str(admin.get("sub") or ""),
            note=payload.note,
            text_value=payload.text,
        )
    except interviewer_playbook.PlaybookError as exc:
        raise _playbook_error(exc) from exc
    await _log_admin_action(
        db,
        admin,
        "DECIDE_PLAYBOOK_ENTRY",
        target_id=entry_id[:36],
        detail={"decision": payload.decision, **result},
    )
    return {"success": True, **result}


@router.get("/ai/evaluation")
async def get_admin_ai_evaluation(
    days: int = Query(7, ge=1, le=90),
    refresh: bool = Query(False),
    _: dict = Depends(require_admin),
) -> dict:
    """Summary of the Langfuse project: judge and reviewer rates, review queue, golden-set runs."""
    return {"success": True, **await ai_overview.evaluation_view(days=days, refresh=refresh)}


@router.get("/ai/speech")
async def get_admin_ai_speech(
    days: int = Query(7, ge=1, le=90),
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """How the interviewer's speech was controlled: guard outcomes, reasons, sources, style."""
    try:
        speech = await interview_speech_control(db, days=days)
    except Exception as exc:  # tables missing until migration 0029 is applied
        await db.rollback()
        raise HTTPException(status_code=503, detail="AI observability tables are not migrated") from exc
    return {"success": True, **speech}


class SpeechCheckReview(BaseModel):
    event_id: int = Field(alias="eventId", ge=1)
    check_index: int = Field(alias="checkIndex", ge=0, le=50)
    verdict: Literal["correct", "wrong", "clear"]


@router.post("/ai/speech/reviews")
async def review_admin_ai_speech_check(
    body: SpeechCheckReview,
    admin: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """An admin's verdict on a refused or edited line: blocked correctly, or a good line blocked."""
    try:
        saved = await review_interview_speech_check(
            db,
            event_id=body.event_id,
            check_index=body.check_index,
            verdict=body.verdict,
            admin_id=str(admin.get("sub") or "") or None,
        )
    except SQLAlchemyError as exc:
        await db.rollback()
        raise HTTPException(status_code=503, detail="AI observability tables are not migrated") from exc
    if saved is None:
        raise HTTPException(status_code=404, detail="Speech check not found")
    return {"success": True, **saved}


_AI_CALL_COLUMNS = (
    "id, session_id, purpose, provider, model, status, duration_ms, input_tokens, output_tokens, "
    "input_chars, output_chars, error_type, error_message, created_at"
)


@router.get("/ai/calls")
async def list_admin_ai_calls(
    purpose: str | None = Query(None, max_length=160),
    status: Literal["ok", "error"] | None = Query(None),
    session_id: str | None = Query(None, alias="sessionId", max_length=36),
    days: int = Query(7, ge=1, le=90),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Model calls, newest first, with a summary over the same filters."""
    clauses = ["created_at >= now() - make_interval(days => :days)"]
    params: dict = {"days": days}
    if purpose:
        clauses.append("purpose ILIKE :purpose")
        params["purpose"] = f"%{purpose}%"
    if status:
        clauses.append("status = :status")
        params["status"] = status
    if session_id:
        clauses.append("session_id = :session_id")
        params["session_id"] = session_id
    where = " AND ".join(clauses)
    try:
        rows = (
            await db.execute(
                text(
                    f"SELECT {_AI_CALL_COLUMNS} FROM ai_call_logs WHERE {where} "
                    "ORDER BY created_at DESC, id DESC LIMIT :limit OFFSET :offset"
                ),
                {**params, "limit": limit, "offset": offset},
            )
        ).mappings().all()
        total = int(
            (await db.execute(text(f"SELECT COUNT(*) FROM ai_call_logs WHERE {where}"), params)).scalar() or 0
        )
        sample = (
            await db.execute(
                text(
                    "SELECT purpose, status, duration_ms, input_tokens, output_tokens FROM ai_call_logs "
                    f"WHERE {where} ORDER BY created_at DESC LIMIT 5000"
                ),
                params,
            )
        ).mappings().all()
    except Exception as exc:
        await db.rollback()
        raise HTTPException(status_code=503, detail="AI observability tables are not migrated") from exc
    return {
        "success": True,
        "total": total,
        # Summary over the latest 5000 matching calls.
        "summary": summarize_ai_calls([dict(r) for r in sample]),
        "calls": [ai_call_view(dict(r)) for r in rows],
    }


@router.get("/ai/calls/{call_id}")
async def get_admin_ai_call(
    call_id: int,
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        row = (
            await db.execute(text("SELECT * FROM ai_call_logs WHERE id = :id"), {"id": call_id})
        ).mappings().one_or_none()
    except Exception as exc:
        await db.rollback()
        raise HTTPException(status_code=503, detail="AI observability tables are not migrated") from exc
    if row is None:
        raise HTTPException(status_code=404, detail="AI call not found")
    return {"success": True, "call": ai_call_view(dict(row))}


@router.get("/matching/runs")
async def list_admin_matching_runs(
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
    status: Literal["all", "running", "completed", "failed"] = Query("all"),
    days: int = Query(7, ge=1, le=90),
    q: str | None = Query(None, max_length=120),
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """CV-JD matching runs, newest first (polled while runs are in progress)."""
    try:
        return await matching_runs.list_runs(db, limit=limit, offset=offset, status=status, days=days, q=q)
    except SQLAlchemyError as exc:  # table missing until migration 0032 is applied
        await db.rollback()
        raise HTTPException(status_code=503, detail=matching_runs.NOT_MIGRATED) from exc


@router.get("/matching/runs/{run_id}")
async def get_admin_matching_run(
    run_id: str,
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """One matching run step by step: requirements, scoring, clarification, AI calls."""
    try:
        detail = await matching_runs.load_run_detail(db, run_id[:36])
    except SQLAlchemyError as exc:  # table missing until migration 0032 is applied
        await db.rollback()
        raise HTTPException(status_code=503, detail=matching_runs.NOT_MIGRATED) from exc
    if detail is None:
        raise HTTPException(status_code=404, detail="Matching run not found")
    return detail


@router.get("/cv-parsing/runs")
async def list_admin_cv_parsing_runs(
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
    status: Literal["all", "running", "completed", "review", "failed"] = Query("all"),
    days: int = Query(7, ge=1, le=90),
    q: str | None = Query(None, max_length=120),
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Uploaded CVs and how far their parsing got, newest first (polled while CVs are parsing)."""
    try:
        return await cv_parsing_runs.list_cv_runs(
            db, limit=limit, offset=offset, status=status, days=days, q=q
        )
    except SQLAlchemyError as exc:  # tables missing until migration 0033 is applied
        await db.rollback()
        raise HTTPException(status_code=503, detail=cv_parsing_runs.NOT_MIGRATED) from exc


@router.get("/cv-parsing/runs/{cv_id}")
async def get_admin_cv_parsing_run(
    cv_id: str,
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """One CV step by step: upload, queue, extraction, parse, quality gate, save, AI calls."""
    try:
        detail = await cv_parsing_runs.load_cv_detail(db, cv_id[:36])
    except SQLAlchemyError as exc:  # tables missing until migration 0033 is applied
        await db.rollback()
        raise HTTPException(status_code=503, detail=cv_parsing_runs.NOT_MIGRATED) from exc
    if detail is None:
        raise HTTPException(status_code=404, detail="CV not found")
    return detail


@router.get("/question-quality")
async def get_admin_question_quality(
    include_demo: bool = Query(True, alias="includeDemo"),
    exclude_email: str | None = Query(None, alias="excludeEmail", max_length=120),
    since: datetime | None = Query(None),
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Question quality per stage and source across stored sessions."""
    report = await interview_question_quality_report(
        db, since=since, include_demo=include_demo, exclude_email_pattern=exclude_email
    )
    return {"success": True, **report}



# --------------------------------------------------------------------------- interventions


async def _log_admin_action(
    db: AsyncSession,
    admin: dict,
    action: str,
    *,
    session_id: str | None = None,
    target_id: str | None = None,
    detail: dict | None = None,
) -> None:
    """Record an admin action in admin_access_logs; never fails the action itself."""
    await record_admin_action(db, admin, action, session_id=session_id, target_id=target_id, detail=detail)


class SessionCloseRequest(BaseModel):
    note: str | None = Field(default=None, max_length=200)


@router.post("/sessions/{session_id}/close")
async def close_admin_session(
    session_id: str,
    payload: SessionCloseRequest | None = None,
    admin: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """End a session stuck open (stored as TECHNICAL_FAILURE; the log names the admin)."""
    note = payload.note if payload else None
    try:
        result = await admin_close_interview_session(db, session_id[:36], note=note)
    except InterviewAdminActionError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.code) from exc
    result["roomClosed"] = await _close_live_room(session_id[:36])
    await _log_admin_action(
        db, admin, "CLOSE_INTERVIEW_SESSION", session_id=session_id[:36], detail={**result, "note": note}
    )
    return {"success": True, **result}


async def _close_live_room(session_id: str) -> bool | None:
    """Tear down the voice/video room of a session the admin closed (None: LiveKit off or unreachable)."""
    from src.core.config import get_settings

    settings = get_settings()
    if not settings.livekit_url or not settings.livekit_api_key or not settings.livekit_api_secret:
        return None
    try:
        from livekit import api
    except ImportError:
        return None
    try:
        return await asyncio.wait_for(
            room_observer.close_room(
                api,
                url=settings.livekit_url,
                key=settings.livekit_api_key,
                secret=settings.livekit_api_secret,
                session_id=session_id,
            ),
            timeout=5,
        )
    except TimeoutError:
        return None


@router.post("/sessions/{session_id}/grade", status_code=202)
async def grade_admin_session(
    session_id: str,
    admin: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Grade a closed session that has no report; runs in the background."""
    try:
        result = await admin_grade_interview_session(db, session_id[:36])
    except InterviewAdminActionError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.code) from exc
    await _log_admin_action(db, admin, "GRADE_INTERVIEW_SESSION", session_id=session_id[:36])
    return {"success": True, **result}


class GradePendingRequest(BaseModel):
    userId: str | None = Field(default=None, max_length=36)


@router.post("/grading-batch", status_code=202)
async def grade_pending_sessions(
    payload: GradePendingRequest | None = None,
    admin: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Grade, one after another, closed sessions without a report (of one user, or of everyone)."""
    user_id = payload.userId if payload else None
    try:
        result = await admin_grade_pending_sessions(db, user_id=user_id)
    except InterviewAdminActionError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.code) from exc
    await _log_admin_action(
        db, admin, "GRADE_PENDING_SESSIONS", target_id=user_id, detail={"queued": result["queued"]}
    )
    return {"success": True, **result}


@router.get("/grading-batch")
async def get_grade_pending_state(
    user_id: str | None = Query(None, alias="userId", max_length=36),
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Progress of the last batch, and how many sessions a batch would grade now."""
    return {
        "success": True,
        "batch": admin_batch_grading_state(user_id),
        "eligible": await admin_count_gradable_sessions(db, user_id=user_id),
    }


@router.post("/cv-parsing/runs/{cv_id}/reparse", status_code=202)
async def reparse_admin_cv(
    cv_id: str,
    admin: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Parse a user's CV again (failed, stuck, or to pick up a parser fix)."""
    try:
        result = await reparse_cv_as_admin(db, cv_id[:36])
    except CvAdminReparseError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.code) from exc
    await _log_admin_action(
        db, admin, "REPARSE_CV", target_id=cv_id[:36], detail={"previousStatus": result["previousStatus"]}
    )
    return {"success": True, **result}


@router.get("/system/config")
async def get_admin_system_config(_: dict = Depends(require_admin)) -> dict:
    """The configuration the backend runs with; read-only, secrets reported as set / not set."""
    return {"success": True, **system_config.config_view()}


@router.get("/system/health")
async def get_admin_system_health(
    _: dict = Depends(require_admin), db: AsyncSession = Depends(get_db)
) -> dict:
    """Database, RabbitMQ queues and workers, LiveKit, voice agent, Langfuse; each check times out alone."""
    return {"success": True, **await system_health.health_view(db)}


@router.get("/practice")
async def get_admin_practice(
    weeks: int = Query(8, ge=2, le=26),
    include_demo: bool = Query(False, alias="includeDemo"),
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Practice week by week: sessions, valid sessions, active users, weekly goals met."""
    return {"success": True, **await practice_stats.practice_view(db, weeks=weeks, include_demo=include_demo)}


@router.get("/users/{user_id}/activity")
async def get_admin_user_activity(
    user_id: str,
    admin: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """One account: CVs, sessions with scores, matching runs, real-interview reports."""
    view = await user_activity.user_activity(db, user_id[:36])
    if view is None:
        raise HTTPException(status_code=404, detail="User not found")
    await _log_admin_action(db, admin, "VIEW_USER_ACTIVITY", target_id=user_id[:36])
    return {"success": True, **view}
