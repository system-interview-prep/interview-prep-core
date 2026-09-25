"""P0 structured interview runtime foundation API.

This module owns the new interview runtime contract. Legacy /ai/session remains
available during migration, but new product flows should create sessions here.
"""

from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.security import current_user
from src.infrastructure.database import get_db

router = APIRouter(prefix="/api/v1/interviews", tags=["interviews"])

InterviewMode = Literal["text", "voice", "video"]


class CreateInterviewSession(BaseModel):
    resume_id: str = Field(alias="resumeId", min_length=1)
    job_id: str = Field(alias="jobId", min_length=1)
    mode: InterviewMode = "text"
    locale: str = Field(default="en-US", min_length=2, max_length=35)
    duration_minutes: int = Field(default=25, alias="durationMinutes", ge=5, le=120)

    model_config = {"populate_by_name": True}


def _session_payload(row: dict) -> dict:
    return {
        "sessionId": row["id"],
        "resumeId": row.get("resume_id"),
        "jobId": row.get("job_id"),
        "mode": row["mode"],
        "locale": row["locale"],
        "durationMinutes": row["duration_minutes"],
        "status": row["status"],
        "startedAt": row["started_at"].isoformat(),
        "endedAt": row["ended_at"].isoformat() if row.get("ended_at") else None,
        "plan": (
            {
                "planId": row["plan_id"],
                "schemaVersion": row["plan_schema_version"],
                "status": row["plan_status"],
            }
            if row.get("plan_id")
            else None
        ),
    }


_SESSION_SELECT = """
    SELECT s.id, s.resume_id, s.job_id, s.mode, s.locale, s.duration_minutes,
           s.status, s.started_at, s.ended_at,
           p.id AS plan_id, p.schema_version AS plan_schema_version,
           p.status AS plan_status
    FROM interview_sessions s
    LEFT JOIN interview_session_plans p ON p.session_id = s.id
"""


async def _owned_session(db: AsyncSession, user_id: str, session_id: str) -> dict:
    result = await db.execute(
        text(_SESSION_SELECT + " WHERE s.id = :sid AND s.user_id = :uid"),
        {"sid": session_id, "uid": user_id},
    )
    row = result.mappings().one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Interview session not found")
    return dict(row)


async def _validate_context(
    db: AsyncSession,
    user_id: str,
    resume_id: str,
    job_id: str,
) -> None:
    resume = await db.scalar(
        text("SELECT 1 FROM user_cvs WHERE id = :id AND user_id = :uid"),
        {"id": resume_id, "uid": user_id},
    )
    if resume is None:
        raise HTTPException(status_code=404, detail="CV not found")

    job = await db.scalar(
        text(
            "SELECT 1 FROM job_descriptions "
            "WHERE id = :id AND item_type = 'JOB_DESCRIPTION'"
        ),
        {"id": job_id},
    )
    if job is None:
        raise HTTPException(status_code=404, detail="Job description not found")


@router.post("/sessions", status_code=status.HTTP_201_CREATED)
async def create_interview_session(
    payload: CreateInterviewSession,
    user: dict = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Create a grounded interview session and an empty draft plan.

    P0 intentionally does not select questions. P1 fills the draft plan from
    canonical CV/JD/matching context; P2 freezes approved question versions.
    """

    await _validate_context(db, user["sub"], payload.resume_id, payload.job_id)

    session_id = str(uuid4())
    plan_id = str(uuid4())
    legacy_type = {"text": "Chat", "voice": "Voice", "video": "Call"}[payload.mode]
    legacy_language = "Vietnamese" if payload.locale.lower().startswith("vi") else "English"

    await db.execute(
        text(
            "INSERT INTO interview_sessions "
            "(id, user_id, type, language, status, resume_id, job_id, mode, locale, duration_minutes) "
            "VALUES (:id, :uid, :type, :language, 'OPEN', :resume_id, :job_id, :mode, :locale, :duration)"
        ),
        {
            "id": session_id,
            "uid": user["sub"],
            "type": legacy_type,
            "language": legacy_language,
            "resume_id": payload.resume_id,
            "job_id": payload.job_id,
            "mode": payload.mode,
            "locale": payload.locale,
            "duration": payload.duration_minutes,
        },
    )
    await db.execute(
        text(
            "INSERT INTO interview_session_plans "
            "(id, session_id, schema_version, status, source_context) "
            "VALUES (:id, :sid, '1.0', 'DRAFT', CAST(:context AS jsonb))"
        ),
        {
            "id": plan_id,
            "sid": session_id,
            "context": json.dumps(
                {
                    "resumeId": payload.resume_id,
                    "jobId": payload.job_id,
                    "source": "p0-session-context",
                }
            ),
        },
    )
    await db.commit()
    return await _owned_session(db, user["sub"], session_id)


@router.get("/sessions/{session_id}")
async def get_interview_session(
    session_id: str,
    user: dict = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await _owned_session(db, user["sub"], session_id)


@router.get("/sessions")
async def list_interview_sessions(
    user: dict = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    result = await db.execute(
        text(
            _SESSION_SELECT
            + " WHERE s.user_id = :uid "
            "AND s.resume_id IS NOT NULL AND s.job_id IS NOT NULL "
            "ORDER BY s.started_at DESC LIMIT 100"
        ),
        {"uid": user["sub"]},
    )
    return {"sessions": [_session_payload(dict(row)) for row in result.mappings().all()]}


@router.post("/sessions/{session_id}/close")
async def close_interview_session(
    session_id: str,
    user: dict = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await _owned_session(db, user["sub"], session_id)
    await db.execute(
        text(
            "UPDATE interview_sessions SET status = 'CLOSED', ended_at = now(), updated_at = now() "
            "WHERE id = :sid AND user_id = :uid"
        ),
        {"sid": session_id, "uid": user["sub"]},
    )
    await db.commit()
    return await _owned_session(db, user["sub"], session_id)
