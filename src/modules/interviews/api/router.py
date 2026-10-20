"""P0 structured interview runtime foundation API.

This module owns the new interview runtime contract. Legacy /ai/session remains
available during migration, but new product flows should create sessions here.
"""

import json
import time
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.security import current_user
from src.core.trace_logging import trace_event
from src.infrastructure.database import get_db
from src.modules.interviews.api.rate_limit import interview_rate_limiter
from src.modules.interviews.application.agent_runtime import database_operations, run_interview_command
from src.modules.interviews.application.session_reaper import CANDIDATE_LEFT_GRACE_MINUTES
from src.modules.interviews.application.chat_runtime import (
    ChatRuntimeError,
    get_chat_runtime,
)
from src.modules.interviews.application.text_runtime import (
    TurnStateError,
    answer_turn,
    ask_turn,
    complete_text_runtime,
    read_text_runtime,
)
from src.modules.interviews.evaluation.evaluation_engine import EvaluationGradingError
from src.modules.interviews.evaluation.evaluation_service import (
    EvaluationServiceError,
    get_session_evaluation,
)
from src.modules.interviews.application.progress import compare_with_previous, load_progress, set_weekly_goal
from src.modules.interviews.application.real_interviews import (
    delete_report,
    list_reports,
    readiness_snapshot,
    save_report,
)
from src.modules.interviews.planning.entry_gate import InterviewEntryBlocked, entry_policy, evaluate_entry
from src.modules.interviews.planning.planner import (
    PlannerPolicyConfigurationError,
    evaluate_session_match,
    read_session_plan,
)
from src.modules.interviews.planning.question_selector import (
    QuestionUnavailableError,
    read_frozen_turns,
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
# Server/Session End Reason: all valid terminal states persisted in DB & returned by GET / runtime
SessionEndReason = Literal[
    "COMPLETED",
    "USER_ENDED",
    "TECHNICAL_FAILURE",
    "HARD_TIMEOUT",
    "FAST_FAIL_TECH",
    # Voice/video: the candidate left the room and did not come back (session reaper).
    "CANDIDATE_LEFT",
]
EndReason = SessionEndReason  # Backward compatibility alias

# Client-Initiated Completion Reason: only user-driven reasons allowed in client request payload.
# System-determined terminal reasons (FAST_FAIL_TECH, HARD_TIMEOUT) must NOT be injected by client.
ClientEndReason = Literal[
    "COMPLETED",
    "USER_ENDED",
    "TECHNICAL_FAILURE",
]


class SendChatMessage(BaseModel):
    client_message_id: str | None = Field(default=None, alias="clientMessageId", min_length=1, max_length=64)
    content: str = Field(min_length=1, max_length=10000)
    telemetry: dict[str, Any] = Field(default_factory=dict)

    model_config = {"populate_by_name": True}


class CompleteChatSession(BaseModel):
    reason: ClientEndReason = Field(default="USER_ENDED")

    model_config = {"populate_by_name": True}


class CloseInterviewSession(BaseModel):
    """Optional body for the generic close endpoint.

    Abandonment defaults to USER_ENDED; a client compensating a failed
    create -> plan -> lock sequence should pass TECHNICAL_FAILURE so the record
    does not blame the candidate for a server-side failure.

    COMPLETED is deliberately NOT accepted here. This endpoint bypasses the
    agenda-coverage invariant enforced by /chat/complete, so allowing it would
    let any client label a half-finished session as a completed interview.
    """

    reason: Literal["USER_ENDED", "TECHNICAL_FAILURE"] = Field(default="USER_ENDED")

    model_config = {"populate_by_name": True}


class ChatPresence(BaseModel):
    """Chat room presence: a heartbeat while the page is open, "left" when it closes."""

    state: Literal["here", "left"] = "here"


class SendVideoTranscript(BaseModel):
    client_message_id: str = Field(alias="clientMessageId", min_length=1, max_length=64)
    final_transcript: str = Field(alias="finalTranscript", min_length=1, max_length=10000)
    duration_seconds: float = Field(default=0.0, alias="durationSeconds", ge=0.0, le=3600)

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
    duration_minutes: int = Field(default=25, alias="durationMinutes", ge=2, le=120)
    # "Luyện vòng này" on the progress page: half of the technical questions
    # come from this round (interview_rounds.FOCUSABLE_AREAS).
    focus_area: Literal["language", "framework", "data", "api", "infra", "ai"] | None = Field(
        default=None, alias="focusArea"
    )

    model_config = {"populate_by_name": True}

    @field_validator("locale")
    @classmethod
    def _vietnamese_only(cls, _value: str) -> str:
        # Interviews are Vietnamese-only; older clients may still send "en-US".
        return "vi-VN"


def _session_payload(row: dict) -> dict:
    return {
        "sessionId": row["id"],
        "resumeId": row.get("resume_id"),
        "jobId": row.get("job_id"),
        "jobTitle": row.get("job_title"),
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
           j.title AS job_title,
           p.id AS plan_id, p.schema_version AS plan_schema_version,
           p.status AS plan_status
    FROM interview_sessions s
    LEFT JOIN job_descriptions j ON j.id = s.job_id
    LEFT JOIN interview_session_plans p ON p.session_id = s.id
"""


# A structured session is created together with its plan row in one
# transaction, so the plan marks it apart from legacy /ai sessions. Do not key
# this on resume_id/job_id: both are ON DELETE SET NULL, and deleting a CV or
# JD must not hide the candidate's past sessions and reports.
_STRUCTURED_SESSION = "p.id IS NOT NULL"


_EXPERIENCE_MODES: dict[str, str] = {
    "question_practice": "text",
    "interview_chat": "text",
    "voice_interview": "voice",
    "video_interview": "video",
}


async def _owned_session(db: AsyncSession, user_id: str, session_id: str) -> dict:
    result = await db.execute(
        text(
            _SESSION_SELECT
            + " WHERE s.id = :sid AND s.user_id = :uid AND " + _STRUCTURED_SESSION
        ),
        {"sid": session_id, "uid": user_id},
    )
    row = result.mappings().one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Interview session not found")
    return dict(row)


def _require_runtime_mode(session: dict, *, mode: str, experience_type: str) -> None:
    if session.get("mode") != mode or session.get("experience_type") != experience_type:
        raise HTTPException(
            status_code=409,
            detail=f"Session is not configured for {experience_type}",
        )


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


def _entry_blocked_http(exc: InterviewEntryBlocked) -> HTTPException:
    # Same shape as question_bank_insufficient: { errorCode, message, details }.
    return HTTPException(status_code=409, detail=exc.decision.detail())


async def _require_match_threshold(db: AsyncSession, user: dict, resume_id: str, job_id: str) -> None:
    """Refuse to create a session whose CV–job match is missing or below the threshold.

    The match comes from the same cache as the fit report page, so this is the
    score the candidate has just seen. No session row is written when blocked.
    """
    try:
        match = await evaluate_session_match(db=db, user=user, resume_id=resume_id, job_id=job_id)
    except ValueError as exc:
        # CV or job not parsed yet: there is no score to enter the room with.
        raise HTTPException(
            status_code=409,
            detail={
                "errorCode": "match_score_unavailable",
                "message": "Chưa chấm được điểm phù hợp giữa CV và vị trí này nên chưa thể vào phòng phỏng vấn.",
                "details": {"reason": str(exc)},
            },
        ) from exc
    decision = evaluate_entry(match)
    trace_event(
        "interviewer",
        "session_entry_checked",
        user_id=user.get("sub"),
        resume_id=resume_id,
        job_id=job_id,
        **decision.trace_fields(),
    )
    if not decision.allowed:
        raise _entry_blocked_http(InterviewEntryBlocked(decision))


class WeeklyGoalPayload(BaseModel):
    weekly_goal: int = Field(alias="weeklyGoal", ge=1, le=7)

    model_config = {"populate_by_name": True}


class RealInterviewPayload(BaseModel):
    """A real interview the candidate reports (application/real_interviews.py)."""

    job_id: str | None = Field(default=None, alias="jobId", max_length=36)
    company: str | None = Field(default=None, max_length=160)
    interview_date: date = Field(alias="date")
    stage: Literal["screening", "technical", "final", "other"]
    result: Literal["passed", "failed", "waiting"]
    asked_areas: list[str] = Field(default_factory=list, alias="askedAreas", max_length=10)
    hard_parts: str | None = Field(default=None, alias="hardParts", max_length=2000)

    model_config = {"populate_by_name": True}


def _tz_offset(value: int) -> int:
    return max(-840, min(840, int(value)))


@router.get("/progress")
async def get_practice_progress(
    tz_offset_minutes: int = Query(0, alias="tzOffsetMinutes", description="Minutes ahead of UTC (Vietnam: 420)"),
    user: dict = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Progress across interviews: skills, jobs, answer habits, weekly streak, milestones."""
    return await load_progress(db, user["sub"], tz_offset_minutes=_tz_offset(tz_offset_minutes))


@router.put("/progress/weekly-goal")
async def update_weekly_goal(
    payload: WeeklyGoalPayload,
    user: dict = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return {"weeklyGoal": await set_weekly_goal(db, user["sub"], payload.weekly_goal)}


@router.get("/progress/real-interviews")
async def get_real_interviews(user: dict = Depends(current_user), db: AsyncSession = Depends(get_db)) -> dict:
    return {"reports": await list_reports(db, user["sub"])}


async def _store_real_interview(
    db: AsyncSession, user: dict, payload: RealInterviewPayload, report_id: str | None
) -> dict:
    if payload.interview_date > datetime.now(UTC).date() + timedelta(days=1):
        raise HTTPException(status_code=422, detail="date is in the future")
    readiness: dict[str, Any] = {}
    if report_id is None:
        readiness = readiness_snapshot(await load_progress(db, user["sub"]), payload.job_id)
    try:
        report = await save_report(
            db,
            user["sub"],
            report_id=report_id,
            job_id=payload.job_id,
            company=payload.company,
            interview_date=payload.interview_date,
            stage=payload.stage,
            result=payload.result,
            asked_areas=payload.asked_areas,
            hard_parts=payload.hard_parts,
            readiness=readiness,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Report not found") from exc
    return {"report": report}


@router.post("/progress/real-interviews", status_code=status.HTTP_201_CREATED)
async def add_real_interview(
    payload: RealInterviewPayload, user: dict = Depends(current_user), db: AsyncSession = Depends(get_db)
) -> dict:
    return await _store_real_interview(db, user, payload, None)


@router.put("/progress/real-interviews/{report_id}")
async def update_real_interview(
    report_id: str,
    payload: RealInterviewPayload,
    user: dict = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await _store_real_interview(db, user, payload, report_id)


@router.delete("/progress/real-interviews/{report_id}")
async def remove_real_interview(
    report_id: str, user: dict = Depends(current_user), db: AsyncSession = Depends(get_db)
) -> dict:
    if not await delete_report(db, user["sub"], report_id):
        raise HTTPException(status_code=404, detail="Report not found")
    return {"deleted": True}


@router.get("/sessions/{session_id}/progress-compare")
async def get_session_progress_compare(
    session_id: str,
    tz_offset_minutes: int = Query(0, alias="tzOffsetMinutes"),
    user: dict = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """"So với lần trước" for the report: null when there is no earlier graded session."""
    await _owned_session(db, user["sub"], session_id)
    progress = await load_progress(db, user["sub"], tz_offset_minutes=_tz_offset(tz_offset_minutes))
    return {"comparison": compare_with_previous(progress, session_id)}


@router.get("/entry-policy")
async def get_interview_entry_policy(user: dict = Depends(current_user)) -> dict:
    """The match threshold for entering an interview room, for the UI to show."""
    return entry_policy().to_payload()


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

    started_at = time.monotonic()
    try:
        expected_mode = _EXPERIENCE_MODES.get(payload.experience_type)
        if expected_mode is not None and payload.mode != expected_mode:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"experienceType '{payload.experience_type}' requires mode "
                    f"'{expected_mode}'"
                ),
            )
        await _validate_context(db, user, payload.resume_id, payload.job_id)
        await _require_match_threshold(db, user, payload.resume_id, payload.job_id)

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
                        "focusArea": payload.focus_area,
                    }
                ),
            },
        )
        await db.commit()
        session_data = _session_payload(await _owned_session(db, user["sub"], session_id))
        trace_event(
            "interviewer",
            "session_created",
            session_id=session_id,
            user_id=user["sub"],
            resume_id=payload.resume_id,
            job_id=payload.job_id,
            mode=payload.mode,
            experience_type=payload.experience_type,
            locale=payload.locale,
            duration_minutes=payload.duration_minutes,
            duration_ms=round((time.monotonic() - started_at) * 1000),
        )
        return session_data
    except Exception as exc:
        trace_event(
            "interviewer",
            "session_creation_failed",
            user_id=user.get("sub"),
            resume_id=payload.resume_id,
            job_id=payload.job_id,
            error_type=type(exc).__name__,
            error_message=str(exc),
            duration_ms=round((time.monotonic() - started_at) * 1000),
        )
        raise


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
            + " WHERE s.user_id = :uid AND " + _STRUCTURED_SESSION
            + " ORDER BY s.started_at DESC LIMIT 100"
        ),
        {"uid": user["sub"]},
    )
    sessions = [_session_payload(dict(row)) for row in result.mappings().all()]
    summaries = await _session_summaries(db, [item["sessionId"] for item in sessions])
    for item in sessions:
        item.update(summaries.get(item["sessionId"], _EMPTY_SUMMARY))
    return {"sessions": sessions}


_EMPTY_SUMMARY = {
    "overallScore": None,
    "evaluatedAt": None,
    "competencyScores": [],
    "totalTurns": 0,
    "answeredTurns": 0,
}


def _competency_summary(raw: object) -> list[dict]:
    """Competency scores of the latest evaluation, as {competency, score, weight}.

    The JSONB column may arrive decoded or as text depending on the driver codec.
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return []
    if not isinstance(raw, list):
        return []
    items: list[dict] = []
    for entry in raw:
        if not isinstance(entry, dict) or not entry.get("competency"):
            continue
        try:
            score = float(entry.get("score"))
            weight = float(entry.get("weight", 1.0))
        except (TypeError, ValueError):
            continue
        items.append({"competency": str(entry["competency"]), "score": score, "weight": weight})
    return items


async def _session_summaries(db: AsyncSession, session_ids: list[str]) -> dict[str, dict]:
    """Score and progress per session for history views (one query, no N+1)."""
    if not session_ids:
        return {}
    result = await db.execute(
        text(
            """
            SELECT s.id AS session_id,
                   ev.overall_score, ev.evaluated_at, ev.competency_scores,
                   COUNT(t.id) AS total_turns,
                   COUNT(t.id) FILTER (WHERE t.status = 'ANSWERED') AS answered_turns
            FROM interview_sessions s
            LEFT JOIN LATERAL (
                SELECT e.overall_score, e.evaluated_at, e.competency_scores
                FROM interview_evaluations e
                WHERE e.session_id = s.id
                ORDER BY e.evaluated_at DESC
                LIMIT 1
            ) ev ON TRUE
            LEFT JOIN interview_turns t ON t.session_id = s.id
            WHERE s.id = ANY(:ids)
            GROUP BY s.id, ev.overall_score, ev.evaluated_at, ev.competency_scores
            """
        ),
        {"ids": session_ids},
    )
    return {
        str(row["session_id"]): {
            "overallScore": float(row["overall_score"]) if row["overall_score"] is not None else None,
            "evaluatedAt": row["evaluated_at"].isoformat() if row["evaluated_at"] else None,
            "competencyScores": _competency_summary(row.get("competency_scores")),
            "totalTurns": int(row["total_turns"] or 0),
            "answeredTurns": int(row["answered_turns"] or 0),
        }
        for row in result.mappings().all()
    }


@router.post("/sessions/{session_id}/plan")
async def build_interview_plan(
    session_id: str,
    user: dict = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    started_at = time.monotonic()
    trace_event("interviewer", "plan_started", session_id=session_id, user_id=user["sub"])
    await interview_rate_limiter.check(user["sub"], "plan")
    session = await _owned_session(db, user["sub"], session_id)
    try:
        result = await run_interview_command(
            session_id=session_id, command="prepare",
            operations=database_operations(db=db, user=user, session_row=session),
        )
        trace_event(
            "interviewer",
            "plan_completed",
            session_id=session_id,
            plan_id=result.get("planId"),
            policy_version=result.get("policyVersion"),
            targets_count=len(result.get("targets", [])),
            duration_ms=round((time.monotonic() - started_at) * 1000),
        )
        return result
    except InterviewEntryBlocked as exc:
        trace_event(
            "interviewer",
            "plan_failed",
            session_id=session_id,
            error_type=type(exc).__name__,
            error_message=str(exc),
            duration_ms=round((time.monotonic() - started_at) * 1000),
        )
        await db.rollback()
        raise _entry_blocked_http(exc) from exc
    except PlannerPolicyConfigurationError as exc:
        # A deployment-level misconfiguration, not a conflict on this session.
        # Keep the offending setting name out of the client response.
        trace_event(
            "interviewer",
            "plan_failed",
            session_id=session_id,
            error_type=type(exc).__name__,
            error_message=str(exc),
            duration_ms=round((time.monotonic() - started_at) * 1000),
        )
        raise HTTPException(
            status_code=500,
            detail="Cấu hình interview planner của hệ thống không hợp lệ.",
        ) from exc
    except RuntimeError as exc:
        trace_event(
            "interviewer",
            "plan_failed",
            session_id=session_id,
            error_type=type(exc).__name__,
            error_message=str(exc),
            duration_ms=round((time.monotonic() - started_at) * 1000),
        )
        # Release the plan row's FOR UPDATE lock now, not when the session closes.
        await db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        trace_event(
            "interviewer",
            "plan_failed",
            session_id=session_id,
            error_type=type(exc).__name__,
            error_message=str(exc),
            duration_ms=round((time.monotonic() - started_at) * 1000),
        )
        await db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        trace_event(
            "interviewer",
            "plan_failed",
            session_id=session_id,
            error_type=type(exc).__name__,
            error_message=str(exc),
            duration_ms=round((time.monotonic() - started_at) * 1000),
        )
        raise


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
    started_at = time.monotonic()
    trace_event("interviewer", "question_selection_started", session_id=session_id)
    await interview_rate_limiter.check(user["sub"], "select")
    session = await _owned_session(db, user["sub"], session_id)
    try:
        result = await run_interview_command(
            session_id=session_id, command="freeze",
            operations=database_operations(db=db, user=user, session_row=session),
        )
        trace_event(
            "interviewer",
            "question_selection_completed",
            session_id=session_id,
            plan_id=result.get("planId"),
            turns_count=len(result.get("turns", [])),
            fallback_count=result.get("fallbackQuestionCount", 0),
            duration_ms=round((time.monotonic() - started_at) * 1000),
        )
        return result
    except QuestionUnavailableError as exc:
        trace_event(
            "interviewer",
            "question_selection_failed",
            session_id=session_id,
            error_type="QuestionUnavailableError",
            error_message=str(exc),
            duration_ms=round((time.monotonic() - started_at) * 1000),
        )
        await db.rollback()
        raise HTTPException(status_code=409, detail=exc.to_payload()) from exc
    except RuntimeError as exc:
        trace_event(
            "interviewer",
            "question_selection_failed",
            session_id=session_id,
            error_type=type(exc).__name__,
            error_message=str(exc),
            duration_ms=round((time.monotonic() - started_at) * 1000),
        )
        await db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        trace_event(
            "interviewer",
            "question_selection_failed",
            session_id=session_id,
            error_type=type(exc).__name__,
            error_message=str(exc),
            duration_ms=round((time.monotonic() - started_at) * 1000),
        )
        await db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        trace_event(
            "interviewer",
            "question_selection_failed",
            session_id=session_id,
            error_type=type(exc).__name__,
            error_message=str(exc),
            duration_ms=round((time.monotonic() - started_at) * 1000),
        )
        raise


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
    started_at = time.monotonic()
    trace_event("interviewer", "text_turn_ask_requested", session_id=session_id, turn_id=turn_id)
    session = await _owned_session(db, user["sub"], session_id)
    try:
        result = await ask_turn(db=db, session_row=session, turn_id=turn_id)
        trace_event(
            "interviewer",
            "text_turn_asked",
            session_id=session_id,
            turn_id=turn_id,
            duration_ms=round((time.monotonic() - started_at) * 1000),
        )
        return result
    except ValueError as exc:
        trace_event(
            "interviewer",
            "text_turn_ask_failed",
            session_id=session_id,
            turn_id=turn_id,
            error_type="ValueError",
            error_message=str(exc),
            duration_ms=round((time.monotonic() - started_at) * 1000),
        )
        await db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except TurnStateError as exc:
        trace_event(
            "interviewer",
            "text_turn_ask_failed",
            session_id=session_id,
            turn_id=turn_id,
            error_type="TurnStateError",
            error_message=str(exc),
            duration_ms=round((time.monotonic() - started_at) * 1000),
        )
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
    started_at = time.monotonic()
    trace_event(
        "interviewer",
        "text_turn_answer_received",
        session_id=session_id,
        turn_id=turn_id,
        answer_length=len(payload.answer_text),
    )
    session = await _owned_session(db, user["sub"], session_id)
    try:
        result = await answer_turn(
            db=db,
            session_row=session,
            turn_id=turn_id,
            answer_text=payload.answer_text,
        )
        trace_event(
            "interviewer",
            "text_turn_answered",
            session_id=session_id,
            turn_id=turn_id,
            duration_ms=round((time.monotonic() - started_at) * 1000),
        )
        return result
    except ValueError as exc:
        trace_event(
            "interviewer",
            "text_turn_answer_failed",
            session_id=session_id,
            turn_id=turn_id,
            error_type="ValueError",
            error_message=str(exc),
            duration_ms=round((time.monotonic() - started_at) * 1000),
        )
        await db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except TurnStateError as exc:
        trace_event(
            "interviewer",
            "text_turn_answer_failed",
            session_id=session_id,
            turn_id=turn_id,
            error_type="TurnStateError",
            error_message=str(exc),
            duration_ms=round((time.monotonic() - started_at) * 1000),
        )
        await db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/sessions/{session_id}/complete")
async def complete_interview_runtime(
    session_id: str,
    user: dict = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    started_at = time.monotonic()
    trace_event("interviewer", "text_runtime_complete_requested", session_id=session_id)
    session = await _owned_session(db, user["sub"], session_id)
    try:
        result = await complete_text_runtime(db=db, session_row=session)
        trace_event(
            "interviewer",
            "text_runtime_completed",
            session_id=session_id,
            duration_ms=round((time.monotonic() - started_at) * 1000),
        )
        return result
    except TurnStateError as exc:
        trace_event(
            "interviewer",
            "text_runtime_complete_failed",
            session_id=session_id,
            error_type="TurnStateError",
            error_message=str(exc),
            duration_ms=round((time.monotonic() - started_at) * 1000),
        )
        await db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/sessions/{session_id}/close")
async def close_interview_session(
    session_id: str,
    payload: CloseInterviewSession | None = None,
    user: dict = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Close a session without running the chat/text completion contract.

    Used for abandonment and for compensating a failed create -> plan -> lock
    sequence. `end_reason` is backfilled so every CLOSED session carries one;
    `COALESCE` keeps a reason already written by the runtime intact, and the
    status guard keeps the call idempotent.
    """
    reason = (payload.reason if payload else None) or "USER_ENDED"
    await _owned_session(db, user["sub"], session_id)
    await db.execute(
        text(
            "UPDATE interview_sessions "
            "SET status = 'CLOSED', end_reason = COALESCE(end_reason, :reason), "
            "ended_at = now(), updated_at = now() "
            "WHERE id = :sid AND user_id = :uid AND status <> 'CLOSED'"
        ),
        {"sid": session_id, "uid": user["sub"], "reason": reason},
    )
    await db.commit()
    trace_event(
        "interviewer", "session_closed", session_id=session_id, user_id=user["sub"], reason=reason
    )
    return _session_payload(await _owned_session(db, user["sub"], session_id))


@router.post("/sessions/{session_id}/chat/start")
async def start_chat(
    session_id: str,
    user: dict = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    started_at = time.monotonic()
    trace_event("interviewer", "chat_start_requested", session_id=session_id)
    session = await _owned_session(db, user["sub"], session_id)
    _require_runtime_mode(session, mode="text", experience_type="interview_chat")
    try:
        result = await run_interview_command(
            session_id=session_id, command="open",
            operations=database_operations(db=db, user=user, session_row=session),
        )
        trace_event(
            "interviewer",
            "chat_start_completed",
            session_id=session_id,
            duration_ms=round((time.monotonic() - started_at) * 1000),
        )
        return result
    except ChatRuntimeError as exc:
        trace_event(
            "interviewer",
            "chat_start_failed",
            session_id=session_id,
            error_type="ChatRuntimeError",
            error_message=str(exc),
            duration_ms=round((time.monotonic() - started_at) * 1000),
        )
        await db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        trace_event(
            "interviewer",
            "chat_start_failed",
            session_id=session_id,
            error_type=type(exc).__name__,
            error_message=str(exc),
            duration_ms=round((time.monotonic() - started_at) * 1000),
        )
        raise


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
    started_at = time.monotonic()
    trace_event(
        "interviewer",
        "candidate_message_received",
        session_id=session_id,
        client_message_id=payload.client_message_id,
        content_length=len(payload.content),
    )
    await interview_rate_limiter.check(user["sub"], "respond")
    session = await _owned_session(db, user["sub"], session_id)
    _require_runtime_mode(session, mode="text", experience_type="interview_chat")
    try:
        message_id = payload.client_message_id or str(uuid4())
        result = await run_interview_command(
            session_id=session_id, command="respond",
            operations=database_operations(db=db, user=user, session_row=session),
            event_id=message_id,
            payload={"client_message_id": message_id,
                     "content": payload.content, "telemetry": payload.telemetry, "modality": "CHAT"},
        )
        asst_type = (result.get("assistantResponse") or {}).get("messageType")
        trace_event(
            "interviewer",
            "candidate_message_processed",
            session_id=session_id,
            client_message_id=payload.client_message_id,
            assistant_message_type=asst_type,
            turn_completed=(result.get("turnStatus") or {}).get("completed"),
            duration_ms=round((time.monotonic() - started_at) * 1000),
        )
        return result
    except ValueError as exc:
        trace_event(
            "interviewer",
            "candidate_message_failed",
            session_id=session_id,
            client_message_id=payload.client_message_id,
            error_type="ValueError",
            error_message=str(exc),
            duration_ms=round((time.monotonic() - started_at) * 1000),
        )
        await db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ChatRuntimeError as exc:
        trace_event(
            "interviewer",
            "candidate_message_failed",
            session_id=session_id,
            client_message_id=payload.client_message_id,
            error_type="ChatRuntimeError",
            error_message=str(exc),
            duration_ms=round((time.monotonic() - started_at) * 1000),
        )
        await db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        trace_event(
            "interviewer",
            "candidate_message_failed",
            session_id=session_id,
            client_message_id=payload.client_message_id,
            error_type=type(exc).__name__,
            error_message=str(exc),
            duration_ms=round((time.monotonic() - started_at) * 1000),
        )
        raise


@router.post("/sessions/{session_id}/video/message")
async def send_video_transcript(
    session_id: str,
    payload: SendVideoTranscript,
    user: dict = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Submit a final video-call transcript to the same interview graph.

    Visual behaviour analysis is deliberately not part of competency scoring.
    """
    await interview_rate_limiter.check(user["sub"], "respond")
    session = await _owned_session(db, user["sub"], session_id)
    _require_runtime_mode(session, mode="video", experience_type="video_interview")
    try:
        return await run_interview_command(
            session_id=session_id, command="respond", event_id=payload.client_message_id,
            operations=database_operations(db=db, user=user, session_row=session),
            payload={"client_message_id": payload.client_message_id,
                     "content": payload.final_transcript, "modality": "VIDEO",
                     "duration_seconds": payload.duration_seconds},
        )
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ChatRuntimeError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/sessions/{session_id}/chat/presence")
async def chat_presence(
    session_id: str,
    payload: ChatPresence,
    user: dict = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Whether the candidate still has the chat room open.

    "here" (on open and every minute) records ``candidate_seen_at`` and clears
    a "left" mark, returning since when they were away; "left" (page closed)
    marks ``candidate_left_at``. The session reaper closes a chat whose
    candidate left, or stopped sending heartbeats, more than
    CANDIDATE_LEFT_GRACE_MINUTES ago, with CANDIDATE_LEFT.
    """
    session = await _owned_session(db, user["sub"], session_id)
    _require_runtime_mode(session, mode="text", experience_type="interview_chat")
    if str(session.get("status") or "").upper() != "OPEN":
        return {"open": False, "awaySince": None, "graceMinutes": CANDIDATE_LEFT_GRACE_MINUTES}
    if payload.state == "left":
        await db.execute(
            text(
                "UPDATE interview_sessions SET metadata = jsonb_set(COALESCE(metadata, '{}'::jsonb), "
                "'{candidate_left_at}', to_jsonb(now()::text)) "
                "WHERE id = :sid AND status = 'OPEN' AND metadata->>'candidate_left_at' IS NULL"
            ),
            {"sid": session_id},
        )
        await db.commit()
        return {"open": True, "awaySince": None, "graceMinutes": CANDIDATE_LEFT_GRACE_MINUTES}
    away = (
        await db.execute(
            text(
                "UPDATE interview_sessions s "
                "SET metadata = (COALESCE(s.metadata, '{}'::jsonb) - 'candidate_left_at') "
                "|| jsonb_build_object('candidate_seen_at', now()::text) "
                "FROM interview_sessions old WHERE s.id = :sid AND old.id = s.id AND s.status = 'OPEN' "
                "RETURNING old.metadata->>'candidate_left_at' AS left_at"
            ),
            {"sid": session_id},
        )
    ).scalar_one_or_none()
    await db.commit()
    return {"open": True, "awaySince": away, "graceMinutes": CANDIDATE_LEFT_GRACE_MINUTES}


@router.post("/sessions/{session_id}/chat/complete")
async def complete_chat(
    session_id: str,
    payload: CompleteChatSession,
    user: dict = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    started_at = time.monotonic()
    trace_event("interviewer", "chat_complete_requested", session_id=session_id, reason=payload.reason)
    session = await _owned_session(db, user["sub"], session_id)
    try:
        result = await run_interview_command(
            session_id=session_id, command="finish",
            operations=database_operations(db=db, user=user, session_row=session),
            payload={"reason": payload.reason}, event_id=payload.reason,
        )
        trace_event(
            "interviewer",
            "chat_completed",
            session_id=session_id,
            reason=payload.reason,
            duration_ms=round((time.monotonic() - started_at) * 1000),
        )
        return result
    except ChatRuntimeError as exc:
        trace_event(
            "interviewer",
            "chat_complete_failed",
            session_id=session_id,
            reason=payload.reason,
            error_type="ChatRuntimeError",
            error_message=str(exc),
            duration_ms=round((time.monotonic() - started_at) * 1000),
        )
        await db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        trace_event(
            "interviewer",
            "chat_complete_failed",
            session_id=session_id,
            reason=payload.reason,
            error_type=type(exc).__name__,
            error_message=str(exc),
            duration_ms=round((time.monotonic() - started_at) * 1000),
        )
        raise


# INTERVIA is a practice tool: a hire/reject label must never reach the
# candidate (contract §1.2.1, SF-04). The decision stays stored for internal
# analysis but is stripped at the candidate-facing boundary.
_HIRING_VERDICT_KEYS = frozenset({"decision_recommendation", "decisionRecommendation", "recommendation"})


def _candidate_evaluation_view(eval_data: dict) -> dict:
    return {key: value for key, value in eval_data.items() if key not in _HIRING_VERDICT_KEYS}


@router.post("/sessions/{session_id}/evaluate")
async def evaluate_session_endpoint(
    session_id: str,
    user: dict = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    started_at = time.monotonic()
    trace_event("interviewer", "evaluation_endpoint_requested", session_id=session_id)
    await interview_rate_limiter.check(user["sub"], "evaluate")
    session = await _owned_session(db, user["sub"], session_id)
    try:
        eval_data = await run_interview_command(
            session_id=session_id, command="score",
            operations=database_operations(db=db, user=user, session_row=session),
        )
        if not eval_data:
            trace_event(
                "interviewer",
                "evaluation_endpoint_failed",
                session_id=session_id,
                error_type="MissingEvaluationReport",
                error_message="Không thể tạo báo cáo đánh giá.",
                duration_ms=round((time.monotonic() - started_at) * 1000),
            )
            raise HTTPException(status_code=500, detail="Không thể tạo báo cáo đánh giá.")
        trace_event(
            "interviewer",
            "evaluation_endpoint_completed",
            session_id=session_id,
            overall_score=eval_data.get("overall_score") or eval_data.get("overallScore"),
            decision=eval_data.get("decision_recommendation") or eval_data.get("recommendation"),
            duration_ms=round((time.monotonic() - started_at) * 1000),
        )
        return _candidate_evaluation_view(eval_data)
    except EvaluationServiceError as exc:
        trace_event(
            "interviewer",
            "evaluation_endpoint_failed",
            session_id=session_id,
            error_type="EvaluationServiceError",
            error_message=str(exc),
            duration_ms=round((time.monotonic() - started_at) * 1000),
        )
        await db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except EvaluationGradingError as exc:
        trace_event(
            "interviewer",
            "evaluation_endpoint_failed",
            session_id=session_id,
            error_type="EvaluationGradingError",
            error_message=str(exc),
            duration_ms=round((time.monotonic() - started_at) * 1000),
        )
        await db.rollback()
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        trace_event(
            "interviewer",
            "evaluation_endpoint_failed",
            session_id=session_id,
            error_type=type(exc).__name__,
            error_message=str(exc),
            duration_ms=round((time.monotonic() - started_at) * 1000),
        )
        raise


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
    return _candidate_evaluation_view(eval_data)


@router.get("/sessions/{session_id}/report")
async def get_session_report_endpoint(
    session_id: str,
    user: dict = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await get_session_evaluation_endpoint(session_id=session_id, user=user, db=db)




class SaveSessionReflection(BaseModel):
    """The candidate's own view of a finished session (debrief step 1 and 3)."""

    # {turnId: 1..4}; 1 = weak … 4 = strong, the same bands the debrief compares against.
    self_ratings: dict[str, int] = Field(default_factory=dict, alias="selfRatings")
    takeaway: str | None = Field(default=None, max_length=1000)

    model_config = {"populate_by_name": True}


def _reflection_payload(session_id: str, row: dict | None) -> dict:
    if row is None:
        return {"sessionId": session_id, "selfRatings": {}, "takeaway": None, "submitted": False, "updatedAt": None}
    return {
        "sessionId": session_id,
        "selfRatings": row["self_ratings"] or {},
        "takeaway": row["takeaway"],
        "submitted": True,
        "updatedAt": row["updated_at"].isoformat() if row.get("updated_at") else None,
    }


@router.get("/sessions/{session_id}/reflection")
async def get_session_reflection(
    session_id: str,
    user: dict = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await _owned_session(db, user["sub"], session_id)
    result = await db.execute(
        text(
            "SELECT self_ratings, takeaway, updated_at FROM interview_session_reflections "
            "WHERE session_id = :sid AND user_id = :uid"
        ),
        {"sid": session_id, "uid": user["sub"]},
    )
    row = result.mappings().one_or_none()
    return _reflection_payload(session_id, dict(row) if row else None)


@router.put("/sessions/{session_id}/reflection")
async def save_session_reflection(
    session_id: str,
    payload: SaveSessionReflection,
    user: dict = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await _owned_session(db, user["sub"], session_id)
    if any(not 1 <= value <= 4 for value in payload.self_ratings.values()):
        raise HTTPException(status_code=422, detail="Tự đánh giá phải nằm trong khoảng 1–4.")
    known = await db.execute(
        text("SELECT id FROM interview_turns WHERE session_id = :sid"), {"sid": session_id}
    )
    turn_ids = {str(row[0]) for row in known.all()}
    unknown = set(payload.self_ratings) - turn_ids
    if unknown:
        raise HTTPException(status_code=422, detail="Có lượt hỏi không thuộc phiên phỏng vấn này.")
    takeaway = (payload.takeaway or "").strip() or None
    result = await db.execute(
        text(
            "INSERT INTO interview_session_reflections (session_id, user_id, self_ratings, takeaway) "
            "VALUES (:sid, :uid, CAST(:ratings AS jsonb), :takeaway) "
            "ON CONFLICT (session_id) DO UPDATE SET self_ratings = EXCLUDED.self_ratings, "
            "takeaway = EXCLUDED.takeaway, updated_at = now() "
            "RETURNING self_ratings, takeaway, updated_at"
        ),
        {
            "sid": session_id,
            "uid": user["sub"],
            "ratings": json.dumps(payload.self_ratings),
            "takeaway": takeaway,
        },
    )
    row = result.mappings().one()
    await db.commit()
    return _reflection_payload(session_id, dict(row))
