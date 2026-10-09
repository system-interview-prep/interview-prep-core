from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.security import current_user
from src.infrastructure.database import get_db

router = APIRouter(prefix="/ai", tags=["sessions"])


class CreateSession(BaseModel):
    type: Literal["Chat", "Voice", "Call"]
    language: str = "English"


def _session(row: dict) -> dict:
    return {
        "id": row["id"],
        "type": row["type"],
        "language": row["language"],
        "status": row["status"],
        "startedAt": row["started_at"].isoformat(),
        "endedAt": row["ended_at"].isoformat() if row["ended_at"] else None,
    }


async def _owned(db: AsyncSession, user_id: str, session_id: str) -> None:
    result = await db.execute(
        text("SELECT 1 FROM interview_sessions WHERE id = :id AND user_id = :uid"),
        {"id": session_id, "uid": user_id},
    )
    if result.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="Session not found")


@router.post("/session")
async def create_session(
    payload: CreateSession, user: dict = Depends(current_user), db: AsyncSession = Depends(get_db)
) -> dict:
    session_id = str(uuid4())
    await db.execute(
        text(
            "INSERT INTO interview_sessions (id, user_id, type, language, status) "
            "VALUES (:id, :uid, :type, :language, 'OPEN')"
        ),
        {
            "id": session_id,
            "uid": user["sub"],
            "type": payload.type,
            "language": payload.language.strip() or "English",
        },
    )
    await db.commit()
    return {"sessionId": session_id}


@router.get("/sessions")
async def list_sessions(user: dict = Depends(current_user), db: AsyncSession = Depends(get_db)) -> dict:
    result = await db.execute(
        text(
            "SELECT id, type, language, status, started_at, ended_at "
            "FROM interview_sessions WHERE user_id = :uid ORDER BY started_at DESC"
        ),
        {"uid": user["sub"]},
    )
    return {"sessions": [_session(row) for row in result.mappings().all()]}


@router.post("/session/{session_id}/close")
async def close_session(
    session_id: str, user: dict = Depends(current_user), db: AsyncSession = Depends(get_db)
) -> dict:
    await _owned(db, user["sub"], session_id)
    # Structured (CV + JD) sessions must end through the interview runtime,
    # which records an end_reason and enforces agenda coverage. Closing them
    # here left end_reason NULL and bypassed every completion guard.
    result = await db.execute(
        text(
            "UPDATE interview_sessions "
            "SET status = 'CLOSED', "
            "ended_at = COALESCE(ended_at, now()), "
            "updated_at = CASE WHEN ended_at IS NULL THEN now() ELSE updated_at END "
            "WHERE id = :id AND user_id = :uid AND resume_id IS NULL AND job_id IS NULL "
            "RETURNING ended_at"
        ),
        {"id": session_id, "uid": user["sub"]},
    )
    ended_at = result.scalar_one_or_none()
    if ended_at is None:
        await db.rollback()
        existing = await db.execute(
            text("SELECT status, ended_at FROM interview_sessions WHERE id = :id AND user_id = :uid"),
            {"id": session_id, "uid": user["sub"]},
        )
        row = existing.mappings().one()
        if row["status"] == "CLOSED" and row["ended_at"] is not None:
            # Already ended through the runtime: report it, change nothing.
            return {"sessionId": session_id, "status": "CLOSED", "endedAt": row["ended_at"].isoformat()}
        raise HTTPException(
            status_code=409,
            detail="Structured interview sessions must be ended through /api/v1/interviews.",
        )
    await db.commit()
    return {"sessionId": session_id, "status": "CLOSED", "endedAt": ended_at.isoformat()}
