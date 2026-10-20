"""Interview Chat Runtime & Controller Engine.

Governs two-way conversational messaging for INTERVIA.
Uses dedicated `interview_chat_messages` table, integrates with P0/P1/P2:
- Uses plan-grounded frozen questions from Question Bank.
- Enforces strict two-phase transaction execution (TX 1 fast write, LLM call outside TX, TX 2 commit).
- Enforces budget cap: max 1 follow-up probe per assessment turn.
- Includes deterministic post-generation validation and safe fallback templates.
- Enforces session linearity and idempotency guarantees.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from time import perf_counter
from typing import Any, Literal
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.pipeline_events import current_session_id
from src.core.trace_logging import trace_event
from src.modules.ai.facade import generate_text
from src.modules.interviews.core.opening_script import build_opening, warmup_question
from src.modules.interviews.core.demo_mode import is_demo_duration
from src.modules.interviews.core.interviewer_voice import (
    CLOSE_COMPLETED_EN,
    CLOSE_COMPLETED_VI,
    USER_ENDED_EN,
    USER_ENDED_VI,
    WRAP_ENDED_EARLY_PARTIAL_EN,
    WRAP_ENDED_EARLY_PARTIAL_VI,
    WRAP_TIMEOUT_PARTIAL_EN,
    WRAP_TIMEOUT_PARTIAL_VI,
    compose_speech,
    first_paragraph,
    last_paragraph,
    pick_variant,
)
from src.modules.interviews.core.interview_engine import (
    SAFE_FALLBACK_PROBE_EN,
    SAFE_FALLBACK_PROBE_VI,
    InterviewCoreEngine,
    pacing_seconds,
    validate_probe_text,
)
from src.modules.interviews.core.interview_types import (
    CandidateTurnInput,
    InterviewerTurnOutput,
    InterviewStage,
    SessionExitReason,
    TurnAction,
)
from src.modules.user_cvs.facade import candidate_name_from_text

_validate_probe_text = validate_probe_text

MessageType = Literal[
    "GREETING",
    "MAIN_QUESTION",
    "CLARIFY",
    "PROBE",
    "CANDIDATE_ANSWER",
    "CANDIDATE_QUESTION",
    "ACKNOWLEDGMENT",
    "WRAP_UP",
    "CONFIRM_ABORT",
]

# Server/Session End Reason: all valid terminal states persisted & exposed by server
EndReason = Literal[
    "COMPLETED",
    "USER_ENDED",
    "TECHNICAL_FAILURE",
    "HARD_TIMEOUT",
    "FAST_FAIL_TECH",
    # Voice/video: the candidate left the room and did not come back (session reaper).
    "CANDIDATE_LEFT",
]

# Client-Initiated Completion Reason: only user-driven reasons allowed in client requests
ClientEndReason = Literal[
    "COMPLETED",
    "USER_ENDED",
    "TECHNICAL_FAILURE",
]


class ChatRuntimeError(RuntimeError):
    pass


# A turn that reached any of these never needs to be asked again.
_SETTLED_TURN_STATUSES = frozenset({"ANSWERED", "COMPLETED", "EVALUATED", "SKIPPED"})
_TECHNICAL_STAGES = frozenset({"VALIDATE", "DEEP_DIVE", "CHALLENGE"})
# A candidate message with no reply after it means another request is still
# generating that reply. Past this age the request is assumed dead (worker
# crash) so the session cannot wedge forever.
_IN_FLIGHT_STALE_SECONDS = 300
# An OPEN session this far past its time budget was abandoned (tab closed,
# network lost). It is closed as HARD_TIMEOUT when the candidate comes back,
# instead of staying OPEN forever.
_ABANDONED_GRACE_SECONDS = 600

_GENERIC_CANDIDATE_NAMES = frozenset(
    {"user", "google user", "candidate", "ứng viên", "admin", "administrator"}
)


def _candidate_name_parts(raw_name: Any) -> tuple[str | None, str | None]:
    """Return a safe full display name and its one-word Vietnamese call name."""
    if not isinstance(raw_name, str):
        return None, None
    normalized = " ".join(raw_name.split()).strip(".,;:!?()[]{}\"'")
    if (
        not normalized
        or len(normalized) > 100
        or normalized.casefold() in _GENERIC_CANDIDATE_NAMES
        or not any(char.isalpha() for char in normalized)
        or any(not (char.isalpha() or char in " -'’") for char in normalized)
    ):
        return None, None
    words = normalized.split()
    return normalized, words[-1].strip("-'’") or None


async def _candidate_names(
    db: AsyncSession, session_id: str
) -> tuple[str | None, str | None]:
    # The candidate is addressed by the name on the CV being interviewed, so a
    # demo account interviewing a demo CV is not called by the account owner's
    # name. Matching still receives a PII-free CanonicalResume: the name is read
    # from the CV's raw text here, only to address the candidate. The account
    # name is the fallback when the CV has no recognisable name.
    cv_text = await db.scalar(
        text(
            "SELECT c.raw_text FROM interview_sessions s "
            "JOIN user_cvs c ON c.id = s.resume_id WHERE s.id = :sid"
        ),
        {"sid": session_id},
    )
    cv_name = candidate_name_from_text(cv_text) if isinstance(cv_text, str) else None
    if cv_name and cv_name.isupper():
        cv_name = " ".join(word.capitalize() for word in cv_name.split())
    full_name, short_name = _candidate_name_parts(cv_name)
    if full_name:
        return full_name, short_name
    raw_name = await db.scalar(
        text(
            "SELECT NULLIF(BTRIM(u.name), '') FROM interview_sessions s "
            "JOIN users u ON u.id = s.user_id WHERE s.id = :sid"
        ),
        {"sid": session_id},
    )
    return _candidate_name_parts(raw_name)


def _personalize_candidate_address(text_value: str, short_name: str | None) -> str:
    """Use the candidate's one-word call name instead of the generic `bạn`."""
    if not short_name:
        return text_value
    return re.sub(r"\b[Bb]ạn\b", short_name, text_value)


def _personalize_opening(
    text_value: str, full_name: str | None, short_name: str | None
) -> str:
    """Use the full name for the first greeting and the short name afterwards."""
    if not full_name:
        return text_value
    personalized, count = re.subn(
        r"\bChào\s+bạn\b", f"Chào {full_name}", text_value, count=1, flags=re.IGNORECASE
    )
    already_named = re.match(
        rf"^Chào\s+{re.escape(full_name)}\b", personalized, flags=re.IGNORECASE
    )
    if count == 0 and not already_named:
        personalized = f"Chào {full_name}, {personalized}"
    greeting_end = personalized.find(",") + 1
    if greeting_end > 0:
        return personalized[:greeting_end] + _personalize_candidate_address(
            personalized[greeting_end:], short_name
        )
    return personalized


def _unfinished_agenda_turns(
    turns: list[dict[str, Any]],
    *,
    current_turn_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return frozen turns that never reached a settled state.

    `current_turn_id` is excluded because the caller is answering that turn right
    now and the in-memory snapshot still carries its pre-answer status.
    """
    return [
        turn
        for turn in turns
        if (current_turn_id is None or str(turn["id"]) != str(current_turn_id))
        and turn.get("status") not in _SETTLED_TURN_STATUSES
    ]


def _provenance_metadata(core_output: Any) -> dict[str, str]:
    """Who wrote an assistant follow-up: the model ("llm") or a fixed fallback.

    PROBE, CLARIFY and closing answers are the only interviewer text not frozen
    from the question bank, so audits need this on the stored message.
    """
    raw = getattr(core_output, "metadata", None) or {}
    out: dict[str, str] = {}
    if raw.get("generated_by"):
        out["generatedBy"] = str(raw["generated_by"])
    if raw.get("missing_aspect"):
        out["missingAspect"] = str(raw["missing_aspect"])
    # gap (answer missed something) or deepen (challenge tier, answer was complete):
    # the progress view counts only gap probes as "needed a follow-up".
    if raw.get("probe_kind"):
        out["probeKind"] = str(raw["probe_kind"])
    return out


async def _displayed_question_text(
    db: AsyncSession, session_row: dict[str, Any], turn: dict[str, Any]
) -> str | None:
    """The question as the interviewer actually said it, for the pinned card.

    Spoken questions are personalised (full name in the greeting, call name
    after it); the frozen snapshot keeps the generic "bạn", so the card showed
    "Chào bạn…" while the candidate heard their own name.
    """
    snapshot = turn.get("question_snapshot") or {}
    question = snapshot.get("questionText")
    if not question or not (session_row.get("locale") or "vi").lower().startswith("vi"):
        return question
    _, short_name = await _candidate_names(db, session_row["id"])
    if snapshot.get("stableKey") == "warmup-intro" or snapshot.get("stage") == "WARM_UP":
        # New sessions ask the warm-up without a greeting (the opening greets);
        # older snapshots still begin "Chào bạn".
        if re.match(r"^\s*Chào\b", question):
            return _personalize_opening(question, short_name, short_name)
        return _personalize_candidate_address(question, short_name)
    return _personalize_candidate_address(question, short_name)


async def _recent_interviewer_lines(db: AsyncSession, session_id: str, limit: int = 4) -> list[str]:
    """The opening sentence of the interviewer's last few messages, newest last.

    Passed to the core so it neither asks the model for, nor falls back to, a
    line it has just said. Best effort: a read failure only loses variety.
    """
    try:
        result = await db.execute(
            text(
                """
                SELECT content FROM interview_chat_messages
                WHERE session_id = :sid AND role = 'assistant'
                ORDER BY sequence DESC
                LIMIT :limit
                """
            ),
            {"sid": session_id, "limit": limit},
        )
        rows = result.mappings().all()
    except Exception:
        return []
    lines: list[str] = []
    for row in reversed(list(rows)[:limit]):
        content = row.get("content") if hasattr(row, "get") else None
        if isinstance(content, str) and content.strip():
            lines.append(first_paragraph(content))
    return lines


def _trace_spoken_turn(
    *,
    session_id: str,
    turn_index: int,
    candidate_text: str,
    modality: str,
    core_output: Any,
    recent_lines: list[str],
    core_ms: int,
) -> None:
    """One record per interviewer turn for the live naturalness monitor.

    Holds the candidate's words and the interviewer's reply (truncated), so,
    like every trace, it is written only when local trace logs are enabled.
    """
    meta = core_output.metadata or {}
    message = str(core_output.message_text or "")
    ack = str(meta.get("acknowledgement") or "") or (first_paragraph(message) if "\n\n" in message else "")
    action = core_output.action.value if hasattr(core_output.action, "value") else str(core_output.action)
    stage = (
        core_output.current_stage.value
        if hasattr(core_output.current_stage, "value")
        else str(core_output.current_stage)
    )
    trace_event(
        "naturalness",
        "turn_spoken",
        session_id=str(session_id),
        turn_index=turn_index,
        modality=str(modality or "CHAT"),
        action=action,
        stage=stage,
        competency=core_output.current_competency,
        candidate=candidate_text[:300],
        interviewer=message[:500],
        acknowledgement=ack[:200] or None,
        ack_source=meta.get("ack_source"),
        ack_reason=meta.get("ack_reason"),
        ack_raw=meta.get("ack_raw"),
        probe_kind=meta.get("probe_kind"),
        generated_by=meta.get("generated_by"),
        # The engine's reading of the answer, for the admin pipeline view.
        intent=meta.get("intent")
        or meta.get("candidate_question_intent")
        or ("CANDIDATE_ABORT" if meta.get("candidate_abort_intent") else None)
        or ("CLARIFY_REQUEST" if meta.get("clarify_used") else None),
        sufficiency=meta.get("sufficiency_status"),
        missing_aspect=str(meta.get("missing_aspect") or "")[:300] or None,
        pre_score=meta.get("pre_score"),
        hard_answer_timeout=meta.get("hard_answer_timeout"),
        answer_chars=len(candidate_text),
        repeats_recent=bool(ack) and any(ack.strip() == str(line).strip() for line in recent_lines),
        core_ms=core_ms,
    )


def _turn_stage(turn: dict[str, Any]) -> str:
    return str((turn.get("question_snapshot") or {}).get("stage") or "UNKNOWN")


def _session_overrun_seconds(session_row: dict[str, Any]) -> int:
    """Budget minus elapsed time; negative once the session is past its budget."""
    started_at = session_row.get("started_at")
    if isinstance(started_at, str):
        try:
            started_at = datetime.fromisoformat(started_at)
        except ValueError:
            return 0
    if not isinstance(started_at, datetime):
        return 0
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=UTC)
    target_seconds = int(session_row.get("duration_minutes") or 25) * 60
    return target_seconds - int((datetime.now(UTC) - started_at).total_seconds())


def _session_remaining_seconds(
    session_row: dict[str, Any],
    *,
    elapsed_override_seconds: float | None = None,
) -> int:
    """Seconds left in the session budget.

    The clock runs from `started_at`, never from how long a single answer took.
    The replay branches used to subtract a per-answer duration from a hardcoded
    900s budget that no session row ever carried, so a 25-minute session
    reported 900 seconds left on its first reply.
    """
    target_seconds = int(session_row.get("duration_minutes") or 25) * 60
    if elapsed_override_seconds is not None and elapsed_override_seconds > 0:
        return max(0, target_seconds - int(elapsed_override_seconds))

    started_at = session_row.get("started_at")
    if isinstance(started_at, str):
        try:
            started_at = datetime.fromisoformat(started_at)
        except ValueError:
            started_at = None
    if not isinstance(started_at, datetime):
        return target_seconds
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=UTC)
    elapsed = max(0, int((datetime.now(UTC) - started_at).total_seconds()))
    return max(0, target_seconds - elapsed)


def _message_payload(row: dict[str, Any]) -> dict[str, Any]:
    created_at = row.get("created_at")
    if isinstance(created_at, datetime):
        created_str = created_at.isoformat()
    elif created_at:
        created_str = str(created_at)
    else:
        created_str = datetime.now(UTC).isoformat()

    return {
        "messageId": row["id"],
        "sessionId": row["session_id"],
        "role": row["role"],
        "messageType": row.get("message_type") or "CANDIDATE_ANSWER",
        "turnId": row.get("turn_id"),
        "sequence": row.get("sequence", 1),
        "content": row.get("content", ""),
        "metadata": row.get("metadata") or {},
        "clientMessageId": row.get("client_message_id"),
        "createdAt": created_str,
    }


async def _get_job_title(db: AsyncSession, job_id: str | None) -> str:
    if not job_id:
        return "Vị trí chuyên môn"
    title = await db.scalar(
        text("SELECT title FROM job_descriptions WHERE id = :job_id"),
        {"job_id": job_id},
    )
    return str(title or "Vị trí ứng tuyển")


async def _get_job_context(db: AsyncSession, job_id: str | None) -> dict[str, Any]:
    """Posting facts the closing Q&A may quote; empty when the job is unknown."""
    if not job_id:
        return {}
    result = await db.execute(
        text(
            """
            SELECT title, company_name, location, work_mode, employment_type, seniority,
                   salary_min, salary_max, salary_currency, salary_negotiable,
                   description, requirements
            FROM job_descriptions WHERE id = :job_id
            """
        ),
        {"job_id": job_id},
    )
    row = result.mappings().one_or_none()
    return dict(row) if row else {}


async def get_chat_runtime(
    db: AsyncSession,
    session_row: dict[str, Any],
) -> dict[str, Any]:
    session_id = session_row["id"]
    job_title = await _get_job_title(db, session_row.get("job_id"))

    # Fetch messages from dedicated interview_chat_messages table
    msg_res = await db.execute(
        text(
            """
            SELECT id, session_id, role, content, metadata, created_at,
                   turn_id, message_type, sequence, client_message_id
            FROM interview_chat_messages
            WHERE session_id = :session_id
            ORDER BY sequence ASC, created_at ASC
            """
        ),
        {"session_id": session_id},
    )
    messages = [_message_payload(dict(r)) for r in msg_res.mappings().all()]

    # Fetch turns
    turns_res = await db.execute(
        text(
            """
            SELECT id, turn_index, status, question_version_id, question_snapshot, answer_text, started_at
            FROM interview_turns
            WHERE session_id = :session_id
            ORDER BY turn_index ASC
            """
        ),
        {"session_id": session_id},
    )
    turns = [dict(r) for r in turns_res.mappings().all()]

    # Turns are not asked in index order: the engine drains DEEP_DIVE before a
    # lower-index CHALLENGE turn and jumps to BEHAVIORAL on the time reserve.
    # The turn being answered is the ASKED one; only fall back to the next
    # PLANNED turn when nothing is asked yet.
    current_turn = next((t for t in turns if t["status"] == "ASKED"), None) or next(
        (t for t in turns if t["status"] == "PLANNED"),
        None,
    )
    current_turn_index = current_turn["turn_index"] if current_turn else len(turns)

    current_turn_probes = [
        m for m in messages
        if current_turn and m.get("turnId") == current_turn["id"] and m.get("role") == "assistant" and m.get("messageType") == "PROBE"
    ]
    current_turn_clarifies = [
        m for m in messages
        if current_turn and m.get("turnId") == current_turn["id"] and m.get("role") == "assistant" and m.get("messageType") == "CLARIFY"
    ]
    is_follow_up = False
    if current_turn and messages:
        last_m = messages[-1]
        if last_m.get("turnId") == current_turn["id"] and last_m.get("role") == "assistant" and last_m.get("messageType") in ("PROBE", "CLARIFY"):
            is_follow_up = True

    turn_stage_map = {t["id"]: (t.get("question_snapshot") or {}).get("stage") for t in turns}
    cv_followup_used = sum(
        1 for m in messages
        if m.get("role") == "assistant" and m.get("messageType") in ("PROBE", "CLARIFY")
        and turn_stage_map.get(m.get("turnId")) == "VALIDATE"
    )
    session_probe_count = sum(
        1 for m in messages
        if m.get("role") == "assistant" and m.get("messageType") == "PROBE"
        and turn_stage_map.get(m.get("turnId")) != "VALIDATE"
    )

    started_at_val = session_row.get("started_at")
    if isinstance(started_at_val, str):
        try:
            started_dt = datetime.fromisoformat(started_at_val)
        except Exception:
            started_dt = datetime.now(UTC)
    elif isinstance(started_at_val, datetime):
        started_dt = started_at_val
    else:
        started_dt = datetime.now(UTC)
    if started_dt.tzinfo is None:
        started_dt = started_dt.replace(tzinfo=UTC)

    target_sec = int(session_row.get("duration_minutes") or 25) * 60
    elapsed_sec = max(0, int((datetime.now(UTC) - started_dt).total_seconds()))
    remaining_sec = max(0, target_sec - elapsed_sec)

    is_awaiting = False
    if session_row["status"] == "OPEN" and messages:
        last_msg = messages[-1]
        if last_msg["role"] == "assistant" and last_msg["messageType"] not in {"WRAP_UP"}:
            is_awaiting = True

    return {
        "sessionId": session_id,
        "sessionStatus": session_row["status"],
        "experienceType": session_row.get("experience_type") or "interview_chat",
        "endReason": session_row.get("end_reason"),
        "jobTitle": job_title,
        "totalTurns": len(turns),
        "currentTurnIndex": current_turn_index,
        "currentTurn": (
            {
                "turnId": current_turn["id"],
                "turnIndex": current_turn["turn_index"],
                "stage": (
                    (current_turn.get("question_snapshot") or {}).get("stage")
                    or ("WARM_UP" if current_turn["turn_index"] == 0 else "VALIDATE" if current_turn["turn_index"] == 1 else "DEEP_DIVE")
                ),
                "competency": (
                    (current_turn.get("question_snapshot") or {})
                    .get("taxonomyTarget", {})
                    .get("label")
                    or (current_turn.get("question_snapshot") or {})
                    .get("target", {})
                    .get("conceptId")
                    or (
                        "Khởi động"
                        if current_turn["turn_index"] == 0
                        else "Thẩm định kinh nghiệm"
                        if current_turn["turn_index"] == 1
                        else "Chuyên môn"
                    )
                ),
                "questionVersionId": str(current_turn.get("question_version_id"))
                if current_turn.get("question_version_id")
                else None,
                "questionType": (current_turn.get("question_snapshot") or {}).get("questionType") or "technical",
                "starterCode": (current_turn.get("question_snapshot") or {}).get("starterCode"),
                "testCasesCode": (current_turn.get("question_snapshot") or {}).get("testCasesCode"),
                "language": (current_turn.get("question_snapshot") or {}).get("language", "python"),
                "isFollowUp": is_follow_up,
                "probeCount": len(current_turn_probes),
                # The room pins the question being answered and times it: the
                # clean bank wording (no acknowledgement prefix), the follow-up
                # if one is open, and the question's own time box.
                "questionText": await _displayed_question_text(db, session_row, current_turn),
                "followUpText": current_turn_probes[-1].get("content") if current_turn_probes else None,
                "startedAt": current_turn["started_at"].isoformat() if current_turn.get("started_at") else None,
                "softAnswerSeconds": (current_turn.get("question_snapshot") or {}).get("softAnswerSeconds"),
                "hardAnswerSeconds": (current_turn.get("question_snapshot") or {}).get("hardAnswerSeconds"),
            }
            if current_turn
            else None
        ),
        "messages": messages,
        "isAwaitingCandidate": is_awaiting,
        "durationMinutes": int(session_row.get("duration_minutes") or 25),
        "startedAt": session_row.get("started_at").isoformat() if session_row.get("started_at") else None,
        "sessionProbeCount": session_probe_count,
        "cvFollowupUsed": bool(cv_followup_used > 0),
        "workingMemory": {
            "candidate_context": {"jobTitle": job_title, "sessionId": session_id},
            "current_stage": (
                (current_turn.get("question_snapshot") or {}).get("stage")
                or ("WARM_UP" if current_turn and current_turn["turn_index"] == 0 else "VALIDATE" if current_turn and current_turn["turn_index"] == 1 else "DEEP_DIVE")
            ) if current_turn else "CLOSED",
            "current_competency": (
                (current_turn.get("question_snapshot") or {}).get("taxonomyTarget", {}).get("label")
                or (current_turn.get("question_snapshot") or {}).get("target", {}).get("conceptId")
            ) if current_turn else None,
            "current_question": (
                (current_turn.get("question_snapshot") or {}).get("questionText")
                or (current_turn.get("question_snapshot") or {}).get("question_text")
            ) if current_turn else None,
            "answered_questions": [t["id"] for t in turns if t["status"] in ("ANSWERED", "COMPLETED", "EVALUATED")],
            "evidence_collected": [t["answer_text"] for t in turns if t.get("answer_text")],
            "missing_evidence": "",
            "probe_count": session_probe_count,
            "cv_followup_used": bool(cv_followup_used > 0),
            "elapsed_time": elapsed_sec,
            "remaining_time": remaining_sec,
        },
        "turns": [
            {
                "turnId": t["id"],
                "turnIndex": t["turn_index"],
                "stage": (
                    (t.get("question_snapshot") or {}).get("stage")
                    or ("WARM_UP" if t["turn_index"] == 0 else "VALIDATE" if t["turn_index"] == 1 else "DEEP_DIVE")
                ),
                "status": t["status"],
                "questionType": (t.get("question_snapshot") or {}).get("questionType") or "technical",
                "starterCode": (t.get("question_snapshot") or {}).get("starterCode"),
                "testCasesCode": (t.get("question_snapshot") or {}).get("testCasesCode"),
                "language": (t.get("question_snapshot") or {}).get("language", "python"),
            }
            for t in turns
        ],
    }


async def start_chat_session(
    db: AsyncSession,
    session_row: dict[str, Any],
) -> dict[str, Any]:
    session_id = session_row["id"]
    # Starting is also the resume/read endpoint used by clients. A closed
    # session must remain readable so refresh/back-navigation can show its
    # persisted transcript instead of turning a successful interview into 409.
    if session_row["status"] == "CLOSED":
        return await get_chat_runtime(db, session_row)
    if session_row.get("plan_status") != "LOCKED":
        raise ChatRuntimeError(
            "P2_NOT_LOCKED: Phiên phỏng vấn phải được chuẩn bị câu hỏi (LOCKED) trước khi bắt đầu chat."
        )

    # Serialize the empty-transcript check with creation of sequence 1. Without
    # this lock two tabs can both observe an empty session and race the opening
    # INSERT, leaving one request as an avoidable 500.
    await db.execute(
        text("SELECT id FROM interview_sessions WHERE id = :sid FOR UPDATE"),
        {"sid": session_id},
    )

    # Check if already started in interview_chat_messages
    existing_count = await db.scalar(
        text("SELECT COUNT(*) FROM interview_chat_messages WHERE session_id = :sid"),
        {"sid": session_id},
    )
    if existing_count and existing_count > 0:
        overdue = -_session_overrun_seconds(session_row)
        if overdue >= _ABANDONED_GRACE_SECONDS:
            await complete_chat_session(db, session_row, reason="HARD_TIMEOUT")
        return await get_chat_runtime(db, session_row)

    # The interview clock starts now, not when the session row was created:
    # planning and question selection (LLM calls) used to eat into the
    # candidate's time budget before the first question was even shown.
    started_at = await db.scalar(
        text("UPDATE interview_sessions SET started_at = now() WHERE id = :sid RETURNING started_at"),
        {"sid": session_id},
    )
    session_row["started_at"] = started_at or datetime.now(UTC)

    # Fetch turns
    turns_res = await db.execute(
        text(
            """
            SELECT id, turn_index, status, question_version_id, question_snapshot
            FROM interview_turns
            WHERE session_id = :session_id
            ORDER BY turn_index ASC
            """
        ),
        {"session_id": session_id},
    )
    turns = [dict(r) for r in turns_res.mappings().all()]
    if not turns:
        raise ChatRuntimeError("QUESTION_SNAPSHOT_MISSING: Không tìm thấy bộ câu hỏi nào cho phiên này.")

    first_turn = turns[0]
    # Update first turn to ASKED
    await db.execute(
        text(
            """
            UPDATE interview_turns
            SET status = 'ASKED', started_at = COALESCE(started_at, now()), updated_at = now()
            WHERE id = :turn_id
            """
        ),
        {"turn_id": first_turn["id"]},
    )

    is_vi = (session_row.get("locale") or "vi").lower().startswith("vi")
    job_title = await _get_job_title(db, session_row.get("job_id"))
    candidate_full_name, candidate_short_name = await _candidate_names(db, session_id)

    # Single opening message: greeting + first turn question
    q_snapshot = first_turn.get("question_snapshot") or {}
    q_text = (
        q_snapshot.get("questionText")
        or q_snapshot.get("question_text")
        or warmup_question(is_vi=is_vi, is_demo=False)
    )

    if re.match(r"^\s*(Chào|Hi|Hello)\b", q_text, flags=re.IGNORECASE):
        # A session frozen before the opening script existed: its warm-up
        # snapshot already greets, so only personalise it.
        opening_text = (
            _personalize_opening(q_text, candidate_short_name, candidate_short_name) if is_vi else q_text
        )
    else:
        opening_text = build_opening(
            is_vi=is_vi,
            mode=str(session_row.get("mode") or "text"),
            duration_minutes=session_row.get("duration_minutes"),
            job_title=job_title,
            # Vietnamese greets by the call name ("Chào Hiền"); the full name
            # ("Chào Nguyễn Thị Thanh Hiền") sounded like a roll call.
            call_name=candidate_short_name,
            planned_stages=[(turn.get("question_snapshot") or {}).get("stage") for turn in turns],
            first_question=q_text,
        )
        if is_vi:
            greeting, _, rest = opening_text.partition(". ")
            opening_text = f"{greeting}. {_personalize_candidate_address(rest, candidate_short_name)}"

    question_id = str(uuid4())
    q_meta = {
        "questionVersionId": str(first_turn["question_version_id"])
        if first_turn.get("question_version_id")
        else None,
        "turnIndex": first_turn.get("turn_index", 0),
        "stage": q_snapshot.get("stage", "WARM_UP"),
        "competency": q_snapshot.get("taxonomyTarget", {}).get("label") or "Giới thiệu & Khởi động",
    }
    await db.execute(
        text(
            """
            INSERT INTO interview_chat_messages
            (id, session_id, role, content, metadata, turn_id, message_type, sequence, created_at)
            VALUES
            (:id, :sid, 'assistant', :content, CAST(:metadata AS jsonb), :turn_id, 'MAIN_QUESTION', 1, now())
            """
        ),
        {
            "id": question_id,
            "sid": session_id,
            "content": opening_text,
            "metadata": json.dumps(q_meta),
            "turn_id": first_turn["id"],
        },
    )
    await db.commit()

    return await get_chat_runtime(db, session_row)


def _format_rubric_criteria(criteria_val: Any) -> str:
    """Chuẩn hóa tiêu chí chấm điểm từ rubric snapshot sang dạng text dễ đọc cho LLM."""
    if isinstance(criteria_val, list):
        parts = []
        for c in criteria_val:
            if isinstance(c, dict):
                name = c.get("name") or c.get("stableKey") or "Tiêu chí"
                desc = c.get("description") or ""
                parts.append(f"- {name}: {desc}")
            else:
                parts.append(f"- {c}")
        return "\n".join(parts)
    elif isinstance(criteria_val, dict):
        return json.dumps(criteria_val, ensure_ascii=False)
    return str(criteria_val or "")


def build_session_state_from_db(
    session_row: dict[str, Any],
    turns: list[dict[str, Any]],
    current_turn: dict[str, Any] | None,
    has_probed: bool,
    *,
    session_probe_count: int = 0,
    cv_followup_used: int = 0,
    clarify_count: int = 0,
    elapsed_override_seconds: float | None = None,
) -> dict[str, Any]:
    """Trích xuất và ánh xạ dữ liệu session/turns sang session_state cho InterviewCoreEngine."""
    session_id = session_row["id"]
    snap = (current_turn.get("question_snapshot") or {}) if current_turn else {}
    current_stage = (snap.get("stage") if current_turn else None) or InterviewStage.DEEP_DIVE.value
    q_text = snap.get("questionText") or snap.get("question_text") or ""
    rubric_criteria = _format_rubric_criteria((snap.get("rubric") or {}).get("criteria", ""))

    asked_question_ids: list[str] = []
    answered_question_ids: list[str] = []
    for t in turns:
        if t.get("status") in {"ASKED", "ANSWERED", "COMPLETED"}:
            if t.get("question_version_id"):
                asked_question_ids.append(str(t["question_version_id"]))
            asked_question_ids.append(str(t["id"]))
        if t.get("status") in {"ANSWERED", "COMPLETED", "EVALUATED"}:
            if t.get("question_version_id"):
                answered_question_ids.append(str(t["question_version_id"]))
            answered_question_ids.append(str(t["id"]))
    if current_turn:
        if current_turn.get("question_version_id"):
            asked_question_ids.append(str(current_turn["question_version_id"]))
            answered_question_ids.append(str(current_turn["question_version_id"]))
        asked_question_ids.append(str(current_turn["id"]))
        answered_question_ids.append(str(current_turn["id"]))
    asked_question_ids = list(set(asked_question_ids))
    answered_question_ids = list(set(answered_question_ids))

    questions_pool: dict[str, list[dict[str, Any]]] = {}
    for t in turns:
        t_snap = t.get("question_snapshot") or {}
        t_stage = t_snap.get("stage") or InterviewStage.DEEP_DIVE.value
        if t_stage not in questions_pool:
            questions_pool[t_stage] = []
        prompt = t_snap.get("questionText") or t_snap.get("question_text") or ""
        q_id = str(t.get("question_version_id") or t["id"])
        comp = (
            (t_snap.get("taxonomyTarget") or {}).get("label")
            or (t_snap.get("target") or {}).get("conceptId")
            or "Chuyên môn"
        )
        questions_pool[t_stage].append({
            "question_id": q_id,
            "question_version_id": str(t.get("question_version_id")) if t.get("question_version_id") else None,
            "competency": comp,
            "stage": t_stage,
            "main_prompt": prompt,
            "spoken_prompt": t_snap.get("spokenText") or None,
            "rubric_criteria": _format_rubric_criteria((t_snap.get("rubric") or {}).get("criteria", "")),
        })

    metadata_json = session_row.get("metadata") or {}
    if isinstance(metadata_json, str):
        try:
            metadata_json = json.loads(metadata_json)
        except Exception:
            metadata_json = {}

    target_duration_minutes = session_row.get("duration_minutes") or 25
    session_seconds = int(target_duration_minutes) * 60
    closing_reserve_seconds = int(
        metadata_json.get("closing_reserve_seconds") or pacing_seconds(60, session_seconds)
    )
    behavioral_reserve_seconds = int(
        metadata_json.get("behavioral_reserve_seconds") or pacing_seconds(180, session_seconds)
    )

    now = datetime.now(UTC)
    started_at_val = session_row.get("started_at")
    if isinstance(started_at_val, str):
        try:
            started_dt = datetime.fromisoformat(started_at_val)
        except Exception:
            started_dt = now
    elif isinstance(started_at_val, datetime):
        started_dt = started_at_val
    else:
        started_dt = now
    if started_dt.tzinfo is None:
        started_dt = started_dt.replace(tzinfo=UTC)

    target_sec = int(target_duration_minutes) * 60
    # `elapsed_override_seconds` is how much of the SESSION has elapsed, not how
    # long the current answer took. Those were conflated before, so any client
    # sending a per-utterance duration reset the session clock on every turn and
    # the interview never reached its closing reserve or hard timeout.
    if elapsed_override_seconds is not None and elapsed_override_seconds > 0:
        elapsed_sec = int(elapsed_override_seconds)
    else:
        elapsed_sec = max(0, int((now - started_dt).total_seconds()))
    remaining_sec = max(0, target_sec - elapsed_sec)

    working_memory = {
        "candidate_context": {"sessionId": session_id, "jobId": session_row.get("job_id")},
        "current_stage": current_stage,
        "current_competency": (
            (snap.get("taxonomyTarget") or {}).get("label")
            or (snap.get("target") or {}).get("conceptId")
            or "Chuyên môn"
        ),
        "current_question": q_text,
        "answered_questions": [t["id"] for t in turns if t.get("status") in {"ANSWERED", "COMPLETED", "EVALUATED"}],
        "evidence_collected": [t["answer_text"] for t in turns if t.get("answer_text")],
        "missing_evidence": "",
        "probe_count": session_probe_count,
        "cv_followup_used": bool(cv_followup_used > 0),
        "elapsed_time": elapsed_sec,
        "remaining_time": remaining_sec,
    }

    return {
        "session_id": session_id,
        "current_stage": current_stage,
        "consecutive_uncooperative": metadata_json.get(
            "consecutive_uncooperative", metadata_json.get("consecutive_fails", 0)
        ),
        "consecutive_fails": metadata_json.get(
            "consecutive_uncooperative", metadata_json.get("consecutive_fails", 0)
        ),
        "current_turn_in_question": 1 if has_probed else 0,
        "target_duration_minutes": target_duration_minutes,
        # Demo sessions are turn-driven (see core.demo_mode).
        "is_demo": is_demo_duration(target_duration_minutes),
        # basic | standard | challenge, frozen on each technical turn from the
        # CV–job match (planning.entry_gate). Challenge also probes good answers.
        "practice_tier": snap.get("practiceTier") or "standard",
        "started_at": session_row.get("started_at"),
        # The session clock, surfaced at the top level because that is where the
        # core engine reads it. Previously only `working_memory` carried it, so
        # the engine silently recomputed elapsed time from `turn_input`, which is
        # per-answer, not per-session.
        "elapsed_time": elapsed_sec,
        "remaining_time": remaining_sec,
        "locale": session_row.get("locale") or "vi-VN",
        "allow_early_exit": metadata_json.get("allow_early_exit", True),
        "asked_question_ids": asked_question_ids,
        "answered_question_ids": answered_question_ids,
        "session_probe_count": session_probe_count,
        "cv_followup_used": cv_followup_used,
        "clarify_count": clarify_count,
        "closing_reserve_seconds": closing_reserve_seconds,
        "behavioral_reserve_seconds": behavioral_reserve_seconds,
        "working_memory": working_memory,
        "current_question_context": {
            "question_id": str(current_turn.get("question_version_id") or current_turn["id"]) if current_turn else "",
            "main_prompt": q_text,
            "spoken_prompt": snap.get("spokenText") or None,
            "competency": working_memory["current_competency"],
            "rubric_criteria": rubric_criteria,
            "hard_answer_seconds": (snap.get("hardAnswerSeconds") or 180),
        } if current_turn else {},
        "questions_pool": questions_pool,
    }


async def process_candidate_message(
    db: AsyncSession,
    session_row: dict[str, Any],
    *,
    client_message_id: str | None,
    content: str,
    telemetry: dict[str, Any] | None = None,
    modality: str = "CHAT",
    duration_seconds: float = 0.0,
    elapsed_override_seconds: float | None = None,
    core_engine: InterviewCoreEngine | None = None,
) -> dict[str, Any]:
    """Process one final candidate turn.

    `duration_seconds` is how long THIS answer took and only drives the
    per-question hard-answer timeout. `elapsed_override_seconds` is how much of
    the whole session has elapsed and is for callers that track it out of band;
    when omitted the session clock comes from `started_at`.
    """
    session_id = session_row["id"]

    if session_row["status"] == "CLOSED":
        raise ChatRuntimeError("ILLEGAL_SESSION_STATE: Phiên phỏng vấn đã kết thúc.")

    # Validate P2 plan is locked/ready
    plan_status = session_row.get("plan_status")
    if not plan_status:
        plan_data = session_row.get("plan_data")
        if isinstance(plan_data, str):
            try:
                plan_data = json.loads(plan_data)
            except Exception:
                plan_data = {}
        if isinstance(plan_data, dict):
            plan_status = plan_data.get("status")
    if plan_status != "LOCKED":
        raise ChatRuntimeError("P2_NOT_LOCKED: Interview plan chưa được khóa (LOCKED).")

    cleaned_content = content.strip()
    if not cleaned_content:
        raise ValueError("Nội dung tin nhắn không được để trống.")

    # Fetch turns
    turns_res = await db.execute(
        text(
            """
            SELECT id, turn_index, status, question_version_id, question_snapshot, answer_text, started_at
            FROM interview_turns
            WHERE session_id = :session_id
            ORDER BY turn_index ASC
            """
        ),
        {"session_id": session_id},
    )
    turns = [dict(r) for r in turns_res.mappings().all()]
    if not turns:
        raise ChatRuntimeError("TURN_NOT_FOUND: Không tìm thấy lượt câu hỏi nào.")

    asked_turns = [t for t in turns if t.get("status") == "ASKED"]
    if len(asked_turns) > 1:
        raise ChatRuntimeError(
            f"INVALID_TURN_STATE: Phát hiện {len(asked_turns)} lượt ở trạng thái ASKED đồng thời."
        )

    if not asked_turns:
        if all(t.get("status") in {"ANSWERED", "COMPLETED", "EVALUATED"} for t in turns):
            raise ChatRuntimeError("CURRENT_TURN_ALREADY_COMPLETED: Tất cả các câu hỏi trong phiên đã hoàn thành.")
        raise ChatRuntimeError("INVALID_TURN_STATE: Không có lượt câu hỏi nào đang ở trạng thái ASKED để trả lời.")

    current_turn = asked_turns[0]

    # Verify link with latest assistant message
    last_asst_msg_res = await db.execute(
        text(
            """
            SELECT id, turn_id, message_type, sequence
            FROM interview_chat_messages
            WHERE session_id = :sid AND role = 'assistant'
            ORDER BY sequence DESC LIMIT 1
            """
        ),
        {"sid": session_id},
    )
    last_asst_msg = last_asst_msg_res.mappings().one_or_none()
    if last_asst_msg and last_asst_msg.get("turn_id"):
        if str(last_asst_msg["turn_id"]) != str(current_turn["id"]):
            raise ChatRuntimeError(
                f"INVALID_TURN_STATE: Lượt trả lời không khớp với câu hỏi gần nhất của trợ lý ({current_turn['id']} != {last_asst_msg['turn_id']})."
            )

    # Invariant: If current turn is BEHAVIORAL, ensure no technical turns remain PLANNED
    curr_snap = current_turn.get("question_snapshot") or {}
    if curr_snap.get("stage") == "BEHAVIORAL":
        has_pending_tech = any(
            t.get("status") == "PLANNED"
            and (t.get("question_snapshot") or {}).get("stage") in _TECHNICAL_STAGES
            for t in turns
        )
        if has_pending_tech:
            raise ChatRuntimeError(
                "INVALID_TURN_STATE: Phát hiện câu hỏi Behavioral được kích hoạt khi còn câu hỏi kỹ thuật chưa hoàn thành."
            )

    if current_turn.get("question_snapshot") is None:
        raise ChatRuntimeError("QUESTION_SNAPSHOT_MISSING: Question snapshot bị thiếu trong turn hiện tại.")

    current_turn_id = current_turn["id"]
    current_turn_index = current_turn["turn_index"]

    # Serialize messages per session. Phase 1 commits before the LLM call, so a
    # second message arriving meanwhile used to take the sequence reserved for
    # the first reply (IntegrityError on its Phase 3, and a retry could return
    # the *candidate's* next message as the assistant reply). The row lock makes
    # the in-flight check and the Phase 1 insert atomic.
    await db.execute(
        text("SELECT id FROM interview_sessions WHERE id = :sid FOR UPDATE"),
        {"sid": session_id},
    )
    recovered_phase1 = False
    existing_user_msg = None
    previous_answer = current_turn.get("answer_text")

    # Check the event id before the generic in-flight guard. Once Phase 1 has
    # committed, retrying this exact event is the only safe way to finish it.
    if client_message_id:
        existing = await db.execute(
            text(
                """
                SELECT id, session_id, role, content, metadata, created_at,
                       turn_id, message_type, sequence, client_message_id,
                       EXTRACT(EPOCH FROM (now() - created_at)) AS age_seconds
                FROM interview_chat_messages
                WHERE session_id = :sid AND client_message_id = :cid
                """
            ),
            {"sid": session_id, "cid": client_message_id},
        )
        existing_user_msg = existing.mappings().one_or_none()
        if existing_user_msg:
            user_seq = existing_user_msg["sequence"]
            asst_res = await db.execute(
                text(
                    """
                    SELECT id, session_id, role, content, metadata, created_at,
                           turn_id, message_type, sequence, client_message_id
                    FROM interview_chat_messages
                    WHERE session_id = :sid AND sequence = :seq
                    """
                ),
                {"sid": session_id, "seq": user_seq + 1},
            )
            asst_msg = asst_res.mappings().one_or_none()
            if asst_msg is None or asst_msg.get("role") != "assistant":
                age_seconds = existing_user_msg.get("age_seconds")
                created_at = existing_user_msg.get("created_at")
                if age_seconds is None and isinstance(created_at, datetime):
                    if created_at.tzinfo is None:
                        created_at = created_at.replace(tzinfo=UTC)
                    age_seconds = (datetime.now(UTC) - created_at).total_seconds()
                if float(age_seconds or 0) < _IN_FLIGHT_STALE_SECONDS:
                    raise ChatRuntimeError(
                        "IN_PROGRESS: Câu trả lời trước vẫn đang được xử lý. Vui lòng chờ phản hồi của người phỏng vấn."
                    )
                if str(existing_user_msg.get("content") or "").strip() != cleaned_content:
                    raise ValueError("EVENT_ID_PAYLOAD_MISMATCH")
                metadata = existing_user_msg.get("metadata") or {}
                if isinstance(metadata, str):
                    try:
                        metadata = json.loads(metadata)
                    except ValueError:
                        metadata = {}
                previous_answer = metadata.get("previousAnswer")
                telemetry = metadata.get("telemetry") or telemetry
                user_msg_id = str(existing_user_msg["id"])
                user_seq = int(existing_user_msg["sequence"])
                asst_seq = user_seq + 1
                recovered_phase1 = True
            else:
                remaining_time = _session_remaining_seconds(
                    session_row, elapsed_override_seconds=elapsed_override_seconds
                )
                return {
                    "sessionId": session_id,
                    "currentStage": (current_turn.get("question_snapshot") or {}).get("stage", "INTRO"),
                    "currentTurnIndex": current_turn_index,
                    "action": asst_msg.get("message_type"),
                    "message": asst_msg.get("content") or "",
                    "remainingTimeSeconds": remaining_time,
                    "probeCount": 0,
                    "completed": False,
                    "userMessage": _message_payload(dict(existing_user_msg)),
                    "assistantResponse": _message_payload(dict(asst_msg)),
                    "turnStatus": {"completed": False},
                    "sessionStatus": session_row["status"],
                }

    if not recovered_phase1:
        last_msg = (
            await db.execute(
                text(
                    """
                    SELECT role, client_message_id,
                           EXTRACT(EPOCH FROM (now() - created_at)) AS age_seconds
                    FROM interview_chat_messages
                    WHERE session_id = :sid
                    ORDER BY sequence DESC LIMIT 1
                    """
                ),
                {"sid": session_id},
            )
        ).mappings().one_or_none()
        if last_msg and last_msg.get("role") == "user":
            if float(last_msg.get("age_seconds") or 0) < _IN_FLIGHT_STALE_SECONDS:
                raise ChatRuntimeError(
                    "IN_PROGRESS: Câu trả lời trước vẫn đang được xử lý. Vui lòng chờ phản hồi của người phỏng vấn."
                )
            raise ChatRuntimeError(
                "RECOVERY_REQUIRED: Câu trả lời trước đã được lưu nhưng chưa có phản hồi. "
                "Vui lòng thử lại đúng tin nhắn đó."
            )

    # =========================================================================
    # PHASE 1: Fast Write Candidate Message (Committed immediately)
    # =========================================================================
    if not recovered_phase1:
        max_seq = await db.scalar(
            text("SELECT COALESCE(MAX(sequence), 0) FROM interview_chat_messages WHERE session_id = :sid"),
            {"sid": session_id},
        )
        user_seq = int(max_seq or 0) + 1
        asst_seq = user_seq + 1
        user_msg_id = str(uuid4())
        try:
            await db.execute(
                text(
                    """
                    INSERT INTO interview_chat_messages
                    (id, session_id, role, content, metadata, turn_id, message_type, sequence, client_message_id, created_at)
                    VALUES
                    (:id, :sid, 'user', :content, CAST(:metadata AS jsonb), :turn_id, 'CANDIDATE_ANSWER', :seq, :cid, now())
                    """
                ),
                {
                    "id": user_msg_id,
                    "sid": session_id,
                    "content": cleaned_content,
                    "metadata": json.dumps(
                        {"telemetry": telemetry or {}, "previousAnswer": previous_answer}
                    ),
                    "turn_id": current_turn_id,
                    "seq": user_seq,
                    "cid": client_message_id,
                },
            )
        except IntegrityError:
            await db.rollback()
            raise ChatRuntimeError(
                "IN_PROGRESS: Câu trả lời đang được xử lý. Vui lòng thử lại sau."
            )

        existing_answer = previous_answer or ""
        new_accumulated = (
            f"{existing_answer}\n\n[Follow-up Answer]: {cleaned_content}"
            if existing_answer
            else cleaned_content
        )
        await db.execute(
            text(
                """
                UPDATE interview_turns
                SET answer_text = :ans, updated_at = now()
                WHERE id = :turn_id
                """
            ),
            {"ans": new_accumulated, "turn_id": current_turn_id},
        )
    # Commit/release the Phase 1 lock before the provider call. Recovered
    # requests skip the writes above, so answer_text is not accumulated twice.
    await db.commit()

    # =========================================================================
    # PHASE 2: Controller & Decision Engine (Outside DB lock)
    # =========================================================================
    is_vi = (session_row.get("locale") or "vi").lower().startswith("vi")
    job_title = await _get_job_title(db, session_row.get("job_id"))
    _, candidate_short_name = await _candidate_names(db, session_id)

    asst_msg_id = str(uuid4())

    # Count all previous probes and clarifies in session and current turn
    probe_msgs_res = await db.execute(
        text(
            """
            SELECT turn_id, message_type, content
            FROM interview_chat_messages
            WHERE session_id = :sid AND role = 'assistant' AND message_type IN ('PROBE', 'CLARIFY')
              -- Repeating an unheard question or answering a question about the
              -- interview is not the question's one clarification.
              AND metadata->>'repeatRequest' IS NULL
              AND metadata->>'candidateQuestionIntent' IS NULL
            ORDER BY sequence
            """
        ),
        {"sid": session_id},
    )
    all_probe_msgs = [dict(r) for r in probe_msgs_res.mappings().all()]
    session_probe_count = len([m for m in all_probe_msgs if m["message_type"] == "PROBE"])
    curr_turn_probes = len([m for m in all_probe_msgs if m["turn_id"] == current_turn_id and m["message_type"] == "PROBE"])
    curr_turn_clarifies = len([m for m in all_probe_msgs if m["turn_id"] == current_turn_id and m["message_type"] == "CLARIFY"])
    has_probed = bool(curr_turn_probes >= 1)

    validate_turn_ids = {t["id"] for t in turns if (t.get("question_snapshot") or {}).get("stage") == "VALIDATE"}
    cv_followup_used = 1 if any(m["turn_id"] in validate_turn_ids and m["message_type"] == "PROBE" for m in all_probe_msgs) else 0

    # Engine events without a session id (guardrail fallbacks, model calls)
    # belong to this interview in the admin pipeline view.
    current_session_id.set(str(session_id))
    # Call Modality-Agnostic Core Engine
    engine = core_engine or InterviewCoreEngine(ai_generator=generate_text)
    session_state = build_session_state_from_db(
        session_row,
        turns,
        current_turn,
        has_probed,
        session_probe_count=session_probe_count,
        cv_followup_used=cv_followup_used,
        clarify_count=curr_turn_clarifies,
        elapsed_override_seconds=elapsed_override_seconds,
    )
    session_state["job_title"] = job_title
    current_turn_probe_texts = [
        m.get("content") for m in all_probe_msgs
        if m["turn_id"] == current_turn_id and m["message_type"] == "PROBE" and m.get("content")
    ]
    if current_turn_probe_texts and session_state.get("current_question_context"):
        # A probe message is "acknowledgement\n\nfollow-up"; the question is the last paragraph.
        session_state["current_question_context"]["follow_up_prompt"] = (
            last_paragraph(current_turn_probe_texts[-1]) or current_turn_probe_texts[-1]
        )
    session_state["recent_interviewer_lines"] = await _recent_interviewer_lines(db, session_id)
    if _turn_stage(current_turn) == "CLOSING":
        session_state["job_context"] = await _get_job_context(db, session_row.get("job_id"))
        # Questions the candidate has sent on this closing turn, including this one.
        questions_asked = await db.scalar(
            text(
                "SELECT COUNT(*) FROM interview_chat_messages "
                "WHERE session_id = :sid AND turn_id = :turn_id AND role = 'user'"
            ),
            {"sid": session_id, "turn_id": current_turn_id},
        )
        closing_started = current_turn.get("started_at")
        qna_elapsed = 0
        if isinstance(closing_started, datetime):
            if closing_started.tzinfo is None:
                closing_started = closing_started.replace(tzinfo=UTC)
            qna_elapsed = max(0, int((datetime.now(UTC) - closing_started).total_seconds()))
        session_state["closing_qna"] = {
            "questions_asked": int(questions_asked or 1),
            "elapsed_seconds": qna_elapsed,
        }

    turn_input = CandidateTurnInput(
        session_id=session_id,
        turn_index=current_turn_index,
        text_content=cleaned_content,
        modality=modality,
        duration_seconds=duration_seconds,
        telemetry=telemetry or {},
    )

    core_started = perf_counter()
    try:
        core_output = await engine.handle_turn(turn_input, session_state)
    except Exception as exc:
        trace_event(
            "interviewer",
            "engine_turn_failed",
            session_id=session_id,
            turn_id=current_turn_id,
            turn_index=current_turn_index,
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        raise

    if is_vi:
        core_output.message_text = _personalize_candidate_address(
            core_output.message_text, candidate_short_name
        )

    _trace_spoken_turn(
        session_id=session_id,
        turn_index=current_turn_index,
        candidate_text=cleaned_content,
        modality=modality,
        core_output=core_output,
        recent_lines=session_state.get("recent_interviewer_lines") or [],
        core_ms=round((perf_counter() - core_started) * 1000),
    )

    trace_event(
        "interviewer",
        "turn_action_dispatched",
        session_id=session_id,
        turn_id=current_turn_id,
        turn_index=current_turn_index,
        action=core_output.action.value if hasattr(core_output.action, "value") else str(core_output.action),
        current_stage=core_output.current_stage.value if hasattr(core_output.current_stage, "value") else str(core_output.current_stage),
        is_session_finished=core_output.is_session_finished,
    )

    # Persist the canonical counter and its deprecated compatibility alias.
    meta = dict(session_row.get("metadata") or {})
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except Exception:
            meta = {}
    meta["consecutive_uncooperative"] = core_output.metadata.get("consecutive_uncooperative", 0)
    meta["consecutive_fails"] = meta["consecutive_uncooperative"]  # Deprecated alias.
    meta["allow_early_exit"] = session_state.get("allow_early_exit", True)
    session_row["metadata"] = meta
    await db.execute(
        text("UPDATE interview_sessions SET metadata = CAST(:meta AS jsonb), updated_at = now() WHERE id = :sid"),
        {"meta": json.dumps(meta), "sid": session_id},
    )

    remaining_time = session_state.get("remaining_time", 0)

    # =========================================================================
    # PHASE 3: Commit Assistant Response & Advance Turn
    # =========================================================================
    # Re-take the session lock released by the Phase 1 commit. If the candidate
    # ended the session while the reply was being generated, do not append a
    # question to a CLOSED session or overwrite its end reason.
    status_after_llm = await db.scalar(
        text("SELECT status FROM interview_sessions WHERE id = :sid FOR UPDATE"),
        {"sid": session_id},
    )
    if status_after_llm == "CLOSED":
        await db.rollback()
        raise ChatRuntimeError("SESSION_CLOSED: Phiên phỏng vấn đã kết thúc trong lúc xử lý câu trả lời.")
    if core_output.action == TurnAction.CONFIRM_ABORT:
        # A stop request is not an answer. Phase 1 stored it as one, so a session
        # ended here graded "em xin dừng" as the answer to the open question (0.0)
        # instead of reporting that question as uncovered.
        await db.execute(
            text(
                """
                UPDATE interview_turns
                SET answer_text = :previous_answer, updated_at = now()
                WHERE id = :turn_id
                """
            ),
            {"previous_answer": previous_answer, "turn_id": current_turn_id},
        )
        await db.execute(
            text(
                """
                UPDATE interview_chat_messages
                SET metadata = metadata || '{"abortRequest": true}'::jsonb
                WHERE id = :message_id
                """
            ),
            {"message_id": user_msg_id},
        )
        await db.execute(
            text(
                """
                INSERT INTO interview_chat_messages
                (id, session_id, role, content, metadata, turn_id, message_type, sequence, created_at)
                VALUES
                (:id, :sid, 'assistant', :content, '{"action": "CONFIRM_ABORT"}'::jsonb, :turn_id, 'CONFIRM_ABORT', :seq, now())
                """
            ),
            {
                "id": asst_msg_id,
                "sid": session_id,
                "content": core_output.message_text,
                "turn_id": current_turn_id,
                "seq": asst_seq,
            },
        )
        await db.commit()
        return {
            "sessionId": session_id,
            "currentStage": core_output.current_stage.value if hasattr(core_output.current_stage, "value") else str(core_output.current_stage),
            "currentTurnIndex": current_turn_index,
            "action": "CONFIRM_ABORT",
            "message": core_output.message_text,
            "remainingTimeSeconds": remaining_time,
            "probeCount": curr_turn_probes,
            "completed": False,
            "userMessage": {
                "messageId": user_msg_id,
                "role": "user",
                "messageType": "CANDIDATE_ANSWER",
                "turnId": current_turn_id,
                "sequence": user_seq,
                "content": cleaned_content,
                "createdAt": datetime.now(UTC).isoformat(),
            },
            "assistantResponse": {
                "messageId": asst_msg_id,
                "role": "assistant",
                "messageType": "CONFIRM_ABORT",
                "turnId": current_turn_id,
                "sequence": asst_seq,
                "content": core_output.message_text,
                "createdAt": datetime.now(UTC).isoformat(),
            },
            "turnStatus": {
                "turnId": current_turn_id,
                "turnIndex": current_turn_index,
                "isFollowUp": False,
                "completed": False,
            },
            "sessionStatus": "OPEN",
        }

    if core_output.action == TurnAction.CLARIFY:
        candidate_question_intent = core_output.metadata.get("candidate_question_intent")
        repeat_request = bool(core_output.metadata.get("repeat_request"))
        user_message_type = "CANDIDATE_QUESTION" if candidate_question_intent else "CLARIFY"
        # A request directed at the interviewer is part of the conversation,
        # but it is not assessment evidence for the current question. Phase 1
        # writes optimistically before the engine runs, so restore the previous
        # accumulated answer and correct the persisted user-message type here.
        await db.execute(
            text(
                """
                UPDATE interview_turns
                SET answer_text = :previous_answer, updated_at = now()
                WHERE id = :turn_id
                """
            ),
            {"previous_answer": previous_answer, "turn_id": current_turn_id},
        )
        await db.execute(
            text(
                """
                UPDATE interview_chat_messages
                SET message_type = :message_type,
                    metadata = metadata || CAST(:extra_metadata AS jsonb)
                WHERE id = :message_id
                """
            ),
            {
                "message_type": user_message_type,
                "extra_metadata": json.dumps(
                    {"candidateQuestionIntent": candidate_question_intent}
                    if candidate_question_intent
                    else {"repeatRequest": True}
                    if repeat_request
                    else {"clarificationRequest": True}
                ),
                "message_id": user_msg_id,
            },
        )
        await db.execute(
            text(
                """
                INSERT INTO interview_chat_messages
                (id, session_id, role, content, metadata, turn_id, message_type, sequence, created_at)
                VALUES
                (:id, :sid, 'assistant', :content, CAST(:metadata AS jsonb), :turn_id, :msg_type, :seq, now())
                """
            ),
            {
                "id": asst_msg_id,
                "sid": session_id,
                "content": core_output.message_text,
                "metadata": json.dumps(
                    (
                        {"candidateQuestionIntent": candidate_question_intent}
                        if candidate_question_intent
                        else {"repeatRequest": True}
                        if repeat_request
                        else {}
                    )
                    | _provenance_metadata(core_output)
                ),
                "turn_id": current_turn_id,
                "msg_type": "CLARIFY",
                "seq": asst_seq,
            },
        )
        await db.commit()
        return {
            "sessionId": session_id,
            "currentStage": core_output.current_stage.value if hasattr(core_output.current_stage, "value") else str(core_output.current_stage),
            "currentTurnIndex": current_turn_index,
            "action": "CLARIFY",
            "message": core_output.message_text,
            "remainingTimeSeconds": remaining_time,
            "probeCount": curr_turn_probes,
            "completed": False,
            "userMessage": {
                "messageId": user_msg_id,
                "role": "user",
                "messageType": user_message_type,
                "turnId": current_turn_id,
                "sequence": user_seq,
                "content": cleaned_content,
                "createdAt": datetime.now(UTC).isoformat(),
            },
            "assistantResponse": {
                "messageId": asst_msg_id,
                "role": "assistant",
                "messageType": "CLARIFY",
                "turnId": current_turn_id,
                "sequence": asst_seq,
                "content": core_output.message_text,
                "createdAt": datetime.now(UTC).isoformat(),
            },
            "turnStatus": {
                "turnId": current_turn_id,
                "turnIndex": current_turn_index,
                "isFollowUp": True,
                "completed": False,
            },
            "sessionStatus": "OPEN",
        }

    if core_output.action == TurnAction.PROBE:
        await db.execute(
            text(
                """
                INSERT INTO interview_chat_messages
                (id, session_id, role, content, metadata, turn_id, message_type, sequence, created_at)
                VALUES
                (:id, :sid, 'assistant', :content, CAST(:metadata AS jsonb), :turn_id, :msg_type, :seq, now())
                """
            ),
            {
                "id": asst_msg_id,
                "sid": session_id,
                "content": core_output.message_text,
                "metadata": json.dumps(_provenance_metadata(core_output)),
                "turn_id": current_turn_id,
                "msg_type": "PROBE",
                "seq": asst_seq,
            },
        )
        await db.commit()
        return {
            "sessionId": session_id,
            "currentStage": core_output.current_stage.value if hasattr(core_output.current_stage, "value") else str(core_output.current_stage),
            "currentTurnIndex": current_turn_index,
            "action": "PROBE",
            "message": core_output.message_text,
            "remainingTimeSeconds": remaining_time,
            "probeCount": curr_turn_probes + 1,
            "completed": False,
            "userMessage": {
                "messageId": user_msg_id,
                "role": "user",
                "messageType": "CANDIDATE_ANSWER",
                "turnId": current_turn_id,
                "sequence": user_seq,
                "content": cleaned_content,
                "createdAt": datetime.now(UTC).isoformat(),
            },
            "assistantResponse": {
                "messageId": asst_msg_id,
                "role": "assistant",
                "messageType": "PROBE",
                "turnId": current_turn_id,
                "sequence": asst_seq,
                "content": core_output.message_text,
                "createdAt": datetime.now(UTC).isoformat(),
            },
            "turnStatus": {
                "turnId": current_turn_id,
                "turnIndex": current_turn_index,
                "isFollowUp": True,
                "completed": False,
            },
            "sessionStatus": "OPEN",
        }

    is_in_closing = (current_turn.get("question_snapshot") or {}).get("stage") == "CLOSING"
    if is_in_closing and not core_output.is_session_finished:
        # Candidate asked a question in Q&A stage: reply directly within the CLOSING turn
        await db.execute(
            text(
                """
                INSERT INTO interview_chat_messages
                (id, session_id, role, content, metadata, turn_id, message_type, sequence, created_at)
                VALUES
                (:id, :sid, 'assistant', :content, CAST(:metadata AS jsonb), :turn_id, 'MAIN_QUESTION', :seq, now())
                """
            ),
            {
                "id": asst_msg_id,
                "sid": session_id,
                "content": core_output.message_text,
                # Typed MAIN_QUESTION for the transcript, but it is the model's
                # answer to the candidate, never a bank question.
                "metadata": json.dumps({"is_qna_answer": True} | _provenance_metadata(core_output)),
                "turn_id": current_turn_id,
                "seq": asst_seq,
            },
        )
        await db.commit()
        return {
            "sessionId": session_id,
            "currentStage": "CLOSING",
            "currentTurnIndex": current_turn_index,
            "action": "ASK_CLOSING",
            "message": core_output.message_text,
            "remainingTimeSeconds": remaining_time,
            "probeCount": curr_turn_probes,
            "completed": False,
            "userMessage": {
                "messageId": user_msg_id,
                "role": "user",
                "messageType": "CANDIDATE_ANSWER",
                "turnId": current_turn_id,
                "sequence": user_seq,
                "content": cleaned_content,
                "createdAt": datetime.now(UTC).isoformat(),
            },
            "assistantResponse": {
                "messageId": asst_msg_id,
                "role": "assistant",
                "messageType": "MAIN_QUESTION",
                "turnId": current_turn_id,
                "sequence": asst_seq,
                "content": core_output.message_text,
                "createdAt": datetime.now(UTC).isoformat(),
            },
            "turnStatus": {
                "turnId": current_turn_id,
                "turnIndex": current_turn_index,
                "isFollowUp": True,
                "completed": False,
            },
            "sessionStatus": "OPEN",
        }

    # Advance to NEXT_TOPIC
    # 1. Complete current turn
    await db.execute(
        text(
            """
            UPDATE interview_turns
            SET status = 'ANSWERED', completed_at = now(), updated_at = now()
            WHERE id = :turn_id
            """
        ),
        {"turn_id": current_turn_id},
    )

    target_qid = core_output.metadata.get("question_id")
    next_turn = None
    if target_qid:
        next_turn = next(
            (
                t
                for t in turns
                if (
                    str(t.get("question_version_id") or "") == str(target_qid)
                    or str(t["id"]) == str(target_qid)
                )
                and t["id"] != current_turn_id
                and t.get("status") == "PLANNED"
            ),
            None,
        )
    if not next_turn:
        # Fallback: pick the lowest-index PLANNED turn that is not the current one.
        # Do NOT filter by turn_index > current_turn_index because multi-competency
        # queues may have pending lower-index technical turns (e.g. a CHALLENGE turn
        # that was skipped by the pacing controller while DEEP_DIVE turns ran first).
        next_turn = next(
            (
                t
                for t in sorted(turns, key=lambda x: x["turn_index"])
                if t["id"] != current_turn_id
                and t.get("status") == "PLANNED"
            ),
            None,
        )

    # ĐẶC BIỆT: NẾU BƯỚC VÀO GIAI ĐOẠN CLOSING MÀ CHƯA CÓ TURN TRONG DB
    if not next_turn and core_output.current_stage == InterviewStage.CLOSING and not core_output.is_session_finished:
        closing_turn_id = str(uuid4())
        # Turn order is not monotonic (the reserve jump asks BEHAVIORAL, the
        # highest index, before lower-index turns), so current+1 can collide
        # with uq_interview_turn_order.
        closing_turn_index = max(t["turn_index"] for t in turns) + 1
        closing_snap = {
            "stage": "CLOSING",
            "questionSource": "closing_template",
            "questionText": core_output.message_text,
            "target": {"conceptId": "closing-qna"},
            "taxonomyTarget": {"label": "Hỏi đáp & Tổng kết"},
        }
        await db.execute(
            text(
                """
                INSERT INTO interview_turns
                (id, session_id, turn_index, status, question_snapshot, started_at, created_at, updated_at)
                VALUES
                (:id, :sid, :tidx, 'ASKED', CAST(:snap AS jsonb), now(), now(), now())
                """
            ),
            {
                "id": closing_turn_id,
                "sid": session_id,
                "tidx": closing_turn_index,
                "snap": json.dumps(closing_snap),
            },
        )
        next_turn = {
            "id": closing_turn_id,
            "session_id": session_id,
            "turn_index": closing_turn_index,
            "status": "ASKED",
            "question_snapshot": closing_snap,
            "question_version_id": None,
        }
        turns.append(next_turn)

    next_stage = (next_turn.get("question_snapshot") or {}).get("stage") if next_turn else None
    if next_stage == "BEHAVIORAL" and not core_output.is_session_finished:
        # The engine jumps to BEHAVIORAL early when the behavioral time reserve
        # is reached. Technical turns left PLANNED at that point will never be
        # asked; settle them as SKIPPED so the BEHAVIORAL invariant holds on the
        # next message and evaluation reports them as uncovered.
        for t in turns:
            if (
                t["id"] != current_turn_id
                and t.get("status") == "PLANNED"
                and (t.get("question_snapshot") or {}).get("stage") in _TECHNICAL_STAGES
            ):
                await db.execute(
                    text(
                        """
                        UPDATE interview_turns
                        SET status = 'SKIPPED', updated_at = now()
                        WHERE id = :turn_id AND status = 'PLANNED'
                        """
                    ),
                    {"turn_id": t["id"]},
                )
                t["status"] = "SKIPPED"

    if next_turn and not core_output.is_session_finished:
        # Start next turn
        await db.execute(
            text(
                """
                UPDATE interview_turns
                SET status = 'ASKED', started_at = COALESCE(started_at, now()), updated_at = now()
                WHERE id = :turn_id
                """
            ),
            {"turn_id": next_turn["id"]},
        )
        next_snap = next_turn.get("question_snapshot") or {}
        # The question as it is said aloud, falling back to the written one.
        next_q_text = (
            next_snap.get("spokenText")
            or next_snap.get("questionText")
            or next_snap.get("question_text")
            or ("Câu hỏi tiếp theo dành cho bạn." if is_vi else "Here is your next question.")
        )
        if is_vi:
            # core_output.message_text is already personalised, so compare like with like.
            next_q_text = _personalize_candidate_address(next_q_text, candidate_short_name)

        # Bridge: the core already said "acknowledgement\n\n[transition] question".
        next_content = core_output.message_text
        if not next_content:
            next_content = next_q_text
        elif next_q_text not in next_content:
            # The core asked a different question than the one the turn queue
            # opens (e.g. a reordered stage): keep its acknowledgement and ask
            # the queued question. This used to insert a stock "Chúng ta hãy
            # cùng chuyển sang câu hỏi tiếp theo nhé:" paragraph.
            next_content = compose_speech(first_paragraph(next_content), next_q_text)
        if is_vi:
            next_content = _personalize_candidate_address(next_content, candidate_short_name)

        q_meta = {
            "questionVersionId": str(next_turn["question_version_id"])
            if next_turn.get("question_version_id")
            else None,
            "turnIndex": next_turn["turn_index"],
        }
        if next_snap.get("stage"):
            q_meta["stage"] = next_snap["stage"]
        if next_snap.get("projectName"):
            q_meta["project_name"] = next_snap["projectName"]
        if next_snap.get("projectId"):
            q_meta["project_id"] = next_snap["projectId"]
        await db.execute(
            text(
                """
                INSERT INTO interview_chat_messages
                (id, session_id, role, content, metadata, turn_id, message_type, sequence, created_at)
                VALUES
                (:id, :sid, 'assistant', :content, CAST(:metadata AS jsonb), :turn_id, 'MAIN_QUESTION', :seq, now())
                """
            ),
            {
                "id": asst_msg_id,
                "sid": session_id,
                "content": next_content,
                "metadata": json.dumps(q_meta),
                "turn_id": next_turn["id"],
                "seq": asst_seq,
            },
        )
        await db.commit()
        next_stage_val = (next_turn.get("question_snapshot") or {}).get("stage") or (core_output.current_stage.value if hasattr(core_output.current_stage, "value") else str(core_output.current_stage))
        return {
            "sessionId": session_id,
            "currentStage": next_stage_val,
            "currentTurnIndex": next_turn["turn_index"],
            "action": "ASK_MAIN",
            "message": next_content,
            "remainingTimeSeconds": remaining_time,
            "probeCount": 0,
            "completed": False,
            "userMessage": {
                "messageId": user_msg_id,
                "role": "user",
                "messageType": "CANDIDATE_ANSWER",
                "turnId": current_turn_id,
                "sequence": user_seq,
                "content": cleaned_content,
                "createdAt": datetime.now(UTC).isoformat(),
            },
            "assistantResponse": {
                "messageId": asst_msg_id,
                "role": "assistant",
                "messageType": "MAIN_QUESTION",
                "turnId": next_turn["id"],
                "sequence": asst_seq,
                "content": next_content,
                "createdAt": datetime.now(UTC).isoformat(),
            },
            "turnStatus": {
                "turnId": next_turn["id"],
                "turnIndex": next_turn["turn_index"],
                "isFollowUp": False,
                "completed": False,
            },
            "sessionStatus": "OPEN",
        }

    # All turns finished or terminated
    end_reason: EndReason = "COMPLETED"
    if core_output.exit_reason:
        if core_output.exit_reason == SessionExitReason.CANDIDATE_ABORT:
            end_reason = "USER_ENDED"
        elif core_output.exit_reason == SessionExitReason.HARD_TIMEOUT:
            end_reason = "HARD_TIMEOUT"
        elif core_output.exit_reason == SessionExitReason.FAST_FAIL_TECH:
            end_reason = "FAST_FAIL_TECH"
        else:
            end_reason = "COMPLETED"
    elif not next_turn:
        end_reason = "COMPLETED"

    downgraded_from_completed = False
    if end_reason == "COMPLETED":
        # The engine owns the decision to stop; only the label can be wrong here.
        # The <= 90s pacing cutoff ends a session while frozen turns are still
        # PLANNED, so calling that "COMPLETED" would misreport coverage.
        #
        # Downgrade the reason instead of raising. Phase 1 has already committed
        # the candidate message, so aborting at this point used to leave the turn
        # ASKED with no assistant reply, and every retry failed the same way with
        # no way out of the room except the manual end button.
        unfinished = _unfinished_agenda_turns(turns, current_turn_id=current_turn_id)
        if unfinished:
            downgraded_from_completed = True
            ran_out_of_time = False
            # Only call it a timeout when time is actually what ran out. The
            # engine can also stop early on a malformed frozen turn or an
            # exhausted stage order, and labelling that HARD_TIMEOUT would hide a
            # real defect behind a pacing explanation.
            reserve_seconds = int(session_state.get("closing_reserve_seconds", 60)) + int(
                session_state.get("behavioral_reserve_seconds", 180)
            )
            ran_out_of_time = int(remaining_time or 0) <= reserve_seconds
            end_reason = "HARD_TIMEOUT" if ran_out_of_time else "TECHNICAL_FAILURE"
            trace_event(
                "interviewer",
                "session_completion_downgraded",
                session_id=session_id,
                turn_id=current_turn_id,
                downgraded_to=end_reason,
                remaining_time_seconds=remaining_time,
                reserve_seconds=reserve_seconds,
                unfinished_turn_count=len(unfinished),
                unfinished_stages=sorted({_turn_stage(turn) for turn in unfinished}),
            )

    wrap_text = "" if downgraded_from_completed else (
        core_output.message_text if core_output.is_session_finished else ""
    )
    if not wrap_text:
        if downgraded_from_completed and ran_out_of_time:
            # Never claim full coverage for a session that stopped early.
            wrap_text = pick_variant(
                WRAP_TIMEOUT_PARTIAL_VI if is_vi else WRAP_TIMEOUT_PARTIAL_EN, f"{session_id}:wrap"
            )
        elif downgraded_from_completed:
            # Stopped early for a reason other than time; do not blame the clock.
            wrap_text = pick_variant(
                WRAP_ENDED_EARLY_PARTIAL_VI if is_vi else WRAP_ENDED_EARLY_PARTIAL_EN, f"{session_id}:wrap"
            )
        else:
            wrap_text = pick_variant(
                CLOSE_COMPLETED_VI if is_vi else CLOSE_COMPLETED_EN, f"{session_id}:wrap"
            )
    if is_vi:
        wrap_text = _personalize_candidate_address(wrap_text, candidate_short_name)

    await db.execute(
        text(
            """
            INSERT INTO interview_chat_messages
            (id, session_id, role, content, metadata, turn_id, message_type, sequence, created_at)
            VALUES
            (:id, :sid, 'assistant', :content, '{}'::jsonb, NULL, 'WRAP_UP', :seq, now())
            """
        ),
        {"id": asst_msg_id, "sid": session_id, "content": wrap_text, "seq": asst_seq},
    )
    await db.execute(
        text(
            """
            UPDATE interview_sessions
            SET status = 'CLOSED', end_reason = :reason, ended_at = now(), updated_at = now()
            WHERE id = :sid
            """
        ),
        {"sid": session_id, "reason": end_reason},
    )
    await db.commit()

    return {
        "sessionId": session_id,
        "currentStage": "CLOSING",
        "currentTurnIndex": current_turn_index,
        "action": "COMPLETE",
        "message": wrap_text,
        "remainingTimeSeconds": remaining_time,
        "probeCount": curr_turn_probes,
        "completed": True,
        "userMessage": {
            "messageId": user_msg_id,
            "role": "user",
            "messageType": "CANDIDATE_ANSWER",
            "turnId": current_turn_id,
            "sequence": user_seq,
            "content": cleaned_content,
            "createdAt": datetime.now(UTC).isoformat(),
        },
        "assistantResponse": {
            "messageId": asst_msg_id,
            "role": "assistant",
            "messageType": "WRAP_UP",
            "turnId": None,
            "sequence": asst_seq,
            "content": wrap_text,
            "createdAt": datetime.now(UTC).isoformat(),
        },
        "turnStatus": {
            "turnId": current_turn_id,
            "turnIndex": current_turn_index,
            "isFollowUp": False,
            "completed": True,
        },
        "sessionStatus": "CLOSED",
        "endReason": end_reason,
    }


async def complete_chat_session(
    db: AsyncSession,
    session_row: dict[str, Any],
    reason: EndReason = "USER_ENDED",
) -> dict[str, Any]:
    session_id = session_row["id"]
    is_vi = (session_row.get("locale") or "vi").lower().startswith("vi")
    _, candidate_short_name = await _candidate_names(db, session_id)

    # Check if session is already closed (either in passed dict or in DB)
    if session_row["status"] == "CLOSED":
        ended_at = session_row.get("ended_at")
        ended_str = (
            ended_at.isoformat()
            if isinstance(ended_at, datetime)
            else datetime.now(UTC).isoformat()
        )
        existing_reason = session_row.get("end_reason") or reason
        return {
            "sessionId": session_id,
            "sessionStatus": "CLOSED",
            "endReason": existing_reason,
            "endedAt": ended_str,
            "summary": _personalize_candidate_address((
                "Phiên phỏng vấn đã kết thúc. Bạn có thể xem lại toàn bộ nội dung trò chuyện."
                if is_vi
                else "The interview session has ended. You can review the full transcript."
            ), candidate_short_name if is_vi else None),
        }

    # Verify directly from DB to prevent race condition / multiple complete calls.
    # The row lock serializes this with a concurrent candidate message.
    cur_session = await db.execute(
        text("SELECT status, end_reason FROM interview_sessions WHERE id = :sid FOR UPDATE"),
        {"sid": session_id},
    )
    row = cur_session.mappings().one_or_none()
    if row and row["status"] == "CLOSED":
        session_row["status"] = "CLOSED"
        existing_reason = row.get("end_reason") or session_row.get("end_reason") or reason
        session_row["end_reason"] = existing_reason
        return {
            "sessionId": session_id,
            "sessionStatus": "CLOSED",
            "endReason": existing_reason,
            "endedAt": datetime.now(UTC).isoformat(),
            "summary": _personalize_candidate_address((
                "Phiên phỏng vấn đã kết thúc. Bạn có thể xem lại toàn bộ nội dung trò chuyện."
                if is_vi
                else "The interview session has ended. You can review the full transcript."
            ), candidate_short_name if is_vi else None),
        }

    # A client may only assert COMPLETED when the frozen agenda really is done.
    # Unlike the runtime auto-close path, nothing has been written yet here, so
    # refusing is safe and keeps a false "COMPLETED" out of the record.
    if reason == "COMPLETED":
        turns_res = await db.execute(
            text(
                """
                SELECT id, turn_index, status, question_snapshot
                FROM interview_turns
                WHERE session_id = :session_id
                ORDER BY turn_index ASC
                """
            ),
            {"session_id": session_id},
        )
        turns = [dict(r) for r in turns_res.mappings().all()]
        unfinished = _unfinished_agenda_turns(turns)
        if unfinished:
            raise ChatRuntimeError(
                "INVALID_SESSION_COMPLETION: Không thể đóng phiên COMPLETED khi còn "
                f"{len(unfinished)} lượt câu hỏi chưa được trả lời "
                f"({', '.join(sorted({_turn_stage(t) for t in unfinished}))})."
            )

    # Check if last message was already WRAP_UP
    last_msg = await db.execute(
        text(
            """
            SELECT message_type FROM interview_chat_messages
            WHERE session_id = :sid ORDER BY sequence DESC LIMIT 1
            """
        ),
        {"sid": session_id},
    )
    row = last_msg.mappings().one_or_none()
    if not row or row["message_type"] != "WRAP_UP":
        max_seq = await db.scalar(
            text("SELECT COALESCE(MAX(sequence), 0) FROM interview_chat_messages WHERE session_id = :sid"),
            {"sid": session_id},
        )
        asst_seq = int(max_seq or 0) + 1
        wrap_text = pick_variant(USER_ENDED_VI if is_vi else USER_ENDED_EN, f"{session_id}:user_ended")
        if is_vi:
            wrap_text = _personalize_candidate_address(wrap_text, candidate_short_name)
        await db.execute(
            text(
                """
                INSERT INTO interview_chat_messages
                (id, session_id, role, content, metadata, turn_id, message_type, sequence, created_at)
                VALUES
                (:id, :sid, 'assistant', :content, '{}'::jsonb, NULL, 'WRAP_UP', :seq, now())
                """
            ),
            {
                "id": str(uuid4()),
                "sid": session_id,
                "content": wrap_text,
                "seq": asst_seq,
            },
        )

    await db.execute(
        text(
            """
            UPDATE interview_sessions
            SET status = 'CLOSED', end_reason = :reason, ended_at = now(), updated_at = now()
            WHERE id = :sid
            """
        ),
        {"sid": session_id, "reason": reason},
    )
    await db.commit()
    session_row["status"] = "CLOSED"

    ended_at = session_row.get("ended_at")
    ended_str = (
        ended_at.isoformat()
        if isinstance(ended_at, datetime)
        else datetime.now(UTC).isoformat()
    )

    summary_text = (
        "Phiên phỏng vấn đã kết thúc. Bạn có thể xem lại toàn bộ nội dung trò chuyện."
        if is_vi
        else "The interview session has ended. You can review the full transcript."
    )
    if is_vi:
        summary_text = _personalize_candidate_address(summary_text, candidate_short_name)

    return {
        "sessionId": session_id,
        "sessionStatus": "CLOSED",
        "endReason": reason,
        "endedAt": ended_str,
        "summary": summary_text,
    }
