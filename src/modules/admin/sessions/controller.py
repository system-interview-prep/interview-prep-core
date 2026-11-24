import asyncio
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.security import require_admin
from src.infrastructure.database import get_db
from src.modules.admin.sessions import room_observer
from src.modules.admin.sessions.repository import (
    access_log_filter as _access_log_filter,
    access_logs as load_access_logs,
    candidate_answers,
    log_candidate_read,
    observer_session,
)
from src.modules.admin.sessions.service import (
    access_logs_view,
    candidate_answers_view,
    list_sessions as list_sessions_view,
)
from src.modules.admin.shared.audit import log_admin_action as _log_admin_action
from src.modules.admin.shared.query_constants import EFFECTIVE_SESSION_MODE
from src.modules.interviews.facade import (
    InterviewAdminActionError, admin_batch_grading_state, admin_close_interview_session,
    admin_count_gradable_sessions, admin_grade_interview_session, admin_grade_pending_sessions,
    interview_question_quality_report, live_interview_sessions, session_pipeline, session_plan_audit,
)

from .schemas import GradePendingRequest, SessionCloseRequest

router = APIRouter(tags=["admin"])








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
    return await list_sessions_view(
        db, mode=mode, search=search, report=report, limit=limit, offset=offset
    )

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

    row = await observer_session(db, session_id)
    refusal = room_observer.observe_refusal(row)
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
        await log_candidate_read(
            db,
            admin=admin,
            action="OBSERVE_INTERVIEW_ROOM",
            session_id=session_id,
            target_id=room_observer.room_name(session_id),
            detail={"mode": row["mode"]},
        )
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
    # The candidate sees a notice that an admin is listening.
    announced = await room_observer.announce_observer(
        api,
        url=settings.livekit_url,
        key=settings.livekit_api_key,
        secret=settings.livekit_api_secret,
        session_id=session_id,
    )
    return {
        "success": True,
        "serverUrl": settings.livekit_url,
        "participantToken": token,
        "roomName": room_observer.room_name(session_id),
        "mode": row["mode"],
        "logged": True,
        "candidateNotified": announced,
    }

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
    rows = await candidate_answers(db, session_id, turn_id)
    try:
        await log_candidate_read(
            db,
            admin=admin,
            action="VIEW_CANDIDATE_ANSWER",
            session_id=session_id,
            target_id=turn_id,
            detail={"answers": len(rows)},
        )
    except Exception as exc:
        await db.rollback()
        raise HTTPException(status_code=503, detail="Access log is not migrated (0030)") from exc
    return candidate_answers_view(rows)


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
        rows, total = await load_access_logs(
            db, where=where, params=params, limit=limit, offset=offset
        )
    except Exception as exc:
        await db.rollback()
        raise HTTPException(status_code=503, detail="Access log is not migrated (0030)") from exc
    return access_logs_view(rows, total)

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
