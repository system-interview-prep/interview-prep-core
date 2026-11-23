"""The admin HTTP endpoints that read and write Langfuse.

Mounted by ``src/modules/admin/__init__.py`` under the ``/admin`` prefix, so the
URLs are exactly what they were before this package existed.

Layering note: this is infrastructure, yet it imports two things from the admin
module - ``log_admin_action`` (the access log) and ``enrich_flagged_turns``
(which joins a trace to the interview turn it belongs to, through our own
database). That is an upward import, accepted so that every Langfuse endpoint
lives in one place. To undo it later: return raw traces here and do the logging
and the join in the admin module.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.security import require_admin
from src.infrastructure.database import get_db
from src.infrastructure.langfuse import reader
from src.modules.admin.ai.service import enrich_flagged_turns
from src.modules.admin.shared.audit import log_admin_action as _log_admin_action

router = APIRouter(tags=["admin"])


class TraceReviewRequest(BaseModel):
    name: str = Field(max_length=64)
    value: bool
    comment: str | None = Field(default=None, max_length=500)
    sessionId: str | None = Field(default=None, max_length=36)


@router.get("/langfuse/traces/{trace_id}")
async def get_langfuse_trace(
    trace_id: str,
    session_id: str | None = Query(None, alias="sessionId", max_length=36),
    admin: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """One turn's model calls and scores, read back from Langfuse."""
    view = await reader.trace_view(trace_id[:64])
    # Sampled turns carry the candidate's (masked) answer: reading it is logged
    # like "Xem c?u tr? l?i".
    if view.get("connected") and any(o.get("input") for o in view.get("observations") or []):
        await _log_admin_action(db, admin, "VIEW_AI_TRACE", session_id=session_id, target_id=trace_id[:64])
    return {"success": True, **view}


@router.post("/langfuse/traces/{trace_id}/reviews", status_code=201)
async def review_langfuse_trace(
    trace_id: str,
    payload: TraceReviewRequest,
    admin: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """A reviewer's verdict on a turn, written to Langfuse as a ``human.*`` score."""
    try:
        await reader.record_review(trace_id[:64], payload.name, payload.value, payload.comment)
    except reader.ReviewScoreError as exc:
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
    return {"success": True, **await reader.session_view(session_id[:36])}

@router.get("/langfuse/flagged")
async def get_flagged_turns(
    days: int = Query(7, ge=1, le=90),
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """The latest turns a rule flagged, with the interview and turn they belong to."""
    view = await enrich_flagged_turns(db, await reader.flagged_turns(days=days))
    return {"success": True, **view}
