"""P0 structured interview runtime foundation API.

This module owns the new interview runtime contract. Legacy /ai/session remains
available during migration, but new product flows should create sessions here.
"""

import json
from typing import Any, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.security import current_user
from src.infrastructure.database import get_db
from src.modules.interviews.chat_runtime import (
    ChatRuntimeError,
    complete_chat_session,
    get_chat_runtime,
    process_candidate_message,
    start_chat_session,
)
from src.modules.interviews.evaluation.evaluation_service import (
    EvaluationServiceError,
    evaluate_closed_session,
    get_session_evaluation,
)
from src.modules.interviews.planner import build_and_persist_session_plan, read_session_plan
from src.modules.interviews.question_selector import (
    QuestionUnavailableError,
    read_frozen_turns,
    select_and_freeze_questions,
)
from src.modules.interviews.text_runtime import (
    TurnStateError,
    answer_turn,
    ask_turn,
    complete_text_runtime,
    read_text_runtime,
)

router = APIRouter(prefix="/api/v1/interviews", tags=["interviews"])

InterviewMode = Literal["text", "voice", "video"]
ExperienceType = Literal[
    "legacy_unstructured",
    "question_practice",
    "voice_interview",
    "video_interview",
    "interview_chat",
]
EndReason = Literal["COMPLETED", "USER_ENDED", "TECHNICAL_FAILURE"]


class SendChatMessage(BaseModel):
    client_message_id: str | None = Field(default=None, alias="clientMessageId")
    content: str = Field(min_length=1, max_length=10000)
    telemetry: dict[str, Any] = Field(default_factory=dict)

    model_config = {"populate_by_name": True}


class CompleteChatSession(BaseModel):
    reason: EndReason = Field(default="USER_ENDED")

    model_config = {"populate_by_name": True}


class SubmitInterviewAnswer(BaseModel):
    answer_text: str = Field(alias="answerText", min_length=1, max_length=20000)

    model_config = {"populate_by_name": True}


class CreateInterviewSession(BaseModel):
    resume_id: str = Field(alias="resumeId", min_length=1)
    job_id: str = Field(alias="jobId", min_length=1)
    mode: InterviewMode = "text"
    experience_type: ExperienceType = Field(default="interview_chat", alias="experienceType")
    locale: str = Field(default="vi-VN", min_length=2, max_length=35)
    duration_minutes: int = Field(default=25, alias="durationMinutes", ge=5, le=120)

    model_config = {"populate_by_name": True}


def _session_payload(row: dict) -> dict:
    return {
        "sessionId": row["id"],
        "resumeId": row.get("resume_id"),
        "jobId": row.get("job_id"),
        "mode": row["mode"],
        "experienceType": row.get("experience_type") or "interview_chat",
        "endReason": row.get("end_reason"),
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
           s.status, s.started_at, s.ended_at, s.experience_type, s.end_reason, s.metadata,
           p.id AS plan_id, p.schema_version AS plan_schema_version,
           p.status AS plan_status
    FROM interview_sessions s
    LEFT JOIN interview_session_plans p ON p.session_id = s.id
"""


async def _owned_session(db: AsyncSession, user_id: str, session_id: str) -> dict:
    result = await db.execute(
        text(
            _SESSION_SELECT
            + " WHERE s.id = :sid AND s.user_id = :uid "
            "AND s.resume_id IS NOT NULL AND s.job_id IS NOT NULL"
        ),
        {"sid": session_id, "uid": user_id},
    )
    row = result.mappings().one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Interview session not found")
    return dict(row)


async def _validate_context(
    db: AsyncSession,
    user: dict,
    resume_id: str,
    job_id: str,
) -> None:
    user_id = user["sub"]
    resume = await db.scalar(
        text("SELECT 1 FROM user_cvs WHERE id = :id AND user_id = :uid"),
        {"id": resume_id, "uid": user_id},
    )
    if resume is None:
        raise HTTPException(status_code=404, detail="CV not found")

    job_filters = ["id = :id", "item_type = 'JOB_DESCRIPTION'"]
    if "ADMIN" not in user.get("roles", []):
        job_filters.append("listing_status = 'ACTIVE'")
    job = await db.scalar(
        text("SELECT 1 FROM job_descriptions WHERE " + " AND ".join(job_filters)),
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

    await _validate_context(db, user, payload.resume_id, payload.job_id)

    session_id = str(uuid4())
    plan_id = str(uuid4())
    legacy_type = {"text": "Chat", "voice": "Voice", "video": "Call"}[payload.mode]
    legacy_language = "Vietnamese" if payload.locale.lower().startswith("vi") else "English"

    await db.execute(
        text(
            "INSERT INTO interview_sessions "
            "(id, user_id, type, language, status, resume_id, job_id, mode, locale, duration_minutes, experience_type) "
            "VALUES (:id, :uid, :type, :language, 'OPEN', :resume_id, :job_id, :mode, :locale, :duration, :experience_type)"
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
            "experience_type": payload.experience_type,
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
    return _session_payload(await _owned_session(db, user["sub"], session_id))


@router.get("/sessions/{session_id}")
async def get_interview_session(
    session_id: str,
    user: dict = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return _session_payload(await _owned_session(db, user["sub"], session_id))


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


@router.post("/sessions/{session_id}/plan")
async def build_interview_plan(
    session_id: str,
    user: dict = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    session = await _owned_session(db, user["sub"], session_id)
    try:
        return await build_and_persist_session_plan(
            db=db,
            user=user,
            session_row=session,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/sessions/{session_id}/plan")
async def get_interview_plan(
    session_id: str,
    user: dict = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    session = await _owned_session(db, user["sub"], session_id)
    try:
        return await read_session_plan(
            db=db,
            plan_id=session["plan_id"],
            session_id=session_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/sessions/{session_id}/questions/select")
async def select_interview_questions(
    session_id: str,
    user: dict = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    session = await _owned_session(db, user["sub"], session_id)
    try:
        return await select_and_freeze_questions(db=db, session_row=session)
    except QuestionUnavailableError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RuntimeError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/sessions/{session_id}/turns")
async def get_interview_turns(
    session_id: str,
    user: dict = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await _owned_session(db, user["sub"], session_id)
    return {
        "sessionId": session_id,
        "turns": await read_frozen_turns(db=db, session_id=session_id),
    }


@router.get("/sessions/{session_id}/runtime")
async def get_text_interview_runtime(
    session_id: str,
    user: dict = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    session = await _owned_session(db, user["sub"], session_id)
    try:
        return await read_text_runtime(db=db, session_row=session)
    except TurnStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/sessions/{session_id}/turns/{turn_id}/ask")
async def ask_interview_turn(
    session_id: str,
    turn_id: str,
    user: dict = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    session = await _owned_session(db, user["sub"], session_id)
    try:
        return await ask_turn(db=db, session_row=session, turn_id=turn_id)
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except TurnStateError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/sessions/{session_id}/turns/{turn_id}/answer")
async def answer_interview_turn(
    session_id: str,
    turn_id: str,
    payload: SubmitInterviewAnswer,
    user: dict = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    session = await _owned_session(db, user["sub"], session_id)
    try:
        return await answer_turn(
            db=db,
            session_row=session,
            turn_id=turn_id,
            answer_text=payload.answer_text,
        )
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except TurnStateError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/sessions/{session_id}/complete")
async def complete_interview_runtime(
    session_id: str,
    user: dict = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    session = await _owned_session(db, user["sub"], session_id)
    try:
        return await complete_text_runtime(db=db, session_row=session)
    except TurnStateError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc


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
            "WHERE id = :sid AND user_id = :uid AND status <> 'CLOSED'"
        ),
        {"sid": session_id, "uid": user["sub"]},
    )
    await db.commit()
    return _session_payload(await _owned_session(db, user["sub"], session_id))


@router.post("/sessions/{session_id}/chat/start")
async def start_chat(
    session_id: str,
    user: dict = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    session = await _owned_session(db, user["sub"], session_id)
    try:
        return await start_chat_session(db=db, session_row=session)
    except ChatRuntimeError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/sessions/{session_id}/chat/runtime")
async def get_chat(
    session_id: str,
    user: dict = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    session = await _owned_session(db, user["sub"], session_id)
    return await get_chat_runtime(db=db, session_row=session)


@router.post("/sessions/{session_id}/chat/message")
async def send_chat(
    session_id: str,
    payload: SendChatMessage,
    user: dict = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    session = await _owned_session(db, user["sub"], session_id)
    try:
        return await process_candidate_message(
            db=db,
            session_row=session,
            client_message_id=payload.client_message_id,
            content=payload.content,
            telemetry=payload.telemetry,
        )
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ChatRuntimeError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/sessions/{session_id}/chat/complete")
async def complete_chat(
    session_id: str,
    payload: CompleteChatSession,
    user: dict = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    session = await _owned_session(db, user["sub"], session_id)
    try:
        return await complete_chat_session(
            db=db,
            session_row=session,
            reason=payload.reason,
        )
    except ChatRuntimeError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/sessions/{session_id}/evaluate")
async def evaluate_session_endpoint(
    session_id: str,
    user: dict = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    session = await _owned_session(db, user["sub"], session_id)
    try:
        await evaluate_closed_session(db=db, session_id=session["id"])
        eval_data = await get_session_evaluation(db=db, session_id=session["id"])
        if not eval_data:
            raise HTTPException(status_code=500, detail="Không thể tạo báo cáo đánh giá.")
        return eval_data
    except EvaluationServiceError as exc:
        await db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/sessions/{session_id}/evaluation")
async def get_session_evaluation_endpoint(
    session_id: str,
    user: dict = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    session = await _owned_session(db, user["sub"], session_id)
    eval_data = await get_session_evaluation(db=db, session_id=session["id"])
    if not eval_data:
        raise HTTPException(status_code=404, detail="Phiên phỏng vấn chưa có kết quả đánh giá.")
    return eval_data


@router.get("/sessions/{session_id}/report")
async def get_session_report_endpoint(
    session_id: str,
    user: dict = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await get_session_evaluation_endpoint(session_id=session_id, user=user, db=db)


