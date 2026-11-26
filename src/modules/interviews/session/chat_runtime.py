"""Interview Chat Runtime & Controller Engine.

Governs two-way conversational messaging for INTERVIA.
Uses dedicated `interview_chat_messages` table, integrates with P0/P1/P2:
- Uses plan-grounded frozen questions from Question Bank.
- Enforces strict two-phase transaction execution (TX 1 fast write, LLM call outside TX, TX 2 commit).
- Enforces budget cap: max 1 follow-up probe per assessment turn.
- Validates generated lines and keeps session actions deterministic.
- Enforces session linearity and idempotency guarantees.
"""

from __future__ import annotations

import json
import re
import unicodedata
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import UTC, datetime
from time import perf_counter
from typing import Any, Literal
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.infrastructure import langfuse
from src.core.pipeline_events import current_session_id
from src.core.trace_logging import trace_event
from src.modules.ai.facade import generate_text
from src.modules.interviews.admin import interviewer_playbook
from src.modules.interviews.core.guardrails import llm_checks
from src.modules.interviews.session.session_deadline import chat_deadline
from src.modules.interviews.core.session_policy.blueprint import InterviewBlueprint, blueprint_from_payload
from src.modules.interviews.core.dialogue import playbook, speech_checks
from src.modules.interviews.core.dialogue.opening import build_opening, warmup_question
from src.modules.interviews.core.session_policy.demo_mode import is_demo_duration
from src.modules.interviews.core.dialogue.policy import (
    first_paragraph,
    last_paragraph,
)
from src.modules.interviews.core.dialogue.topics import generic_transition
from src.modules.interviews.agents.interviewer import record as agent_record
# Turn context construction moved to the Interviewer Agent (V2 spec §10). Both
# names are re-exported here because the Voice Lab, the runtime and several
# tests import them from this module.
from src.modules.interviews.agents.interviewer.context import (  # noqa: F401
    build_session_state_from_db,
    session_blueprint,
)
from src.modules.interviews.memory import candidate_memory
from src.modules.interviews.agents.interviewer.graph import run_turn
from src.modules.interviews.agents.interviewer.engine import InterviewCoreEngine
from src.modules.interviews.core.conversation.turn_engine import (
    pacing_seconds,
    validate_probe_text,
)
from src.modules.interviews.core.contracts.interview import (
    CandidateTurnInput,
    InterviewStage,
    SessionExitReason,
    TurnAction,
)
from src.modules.user_cvs.facade import candidate_name_from_text
from src.modules.interviews.realtime.anticipation.validator import PreviewKey, validate_preview
from src.modules.interviews.session.preview_authority import authoritative_preview_key

_validate_probe_text = validate_probe_text


def _is_simple_transition_ack(text_value: str) -> bool:
    """Recognize a short readiness response without swallowing a real answer."""
    normalized = unicodedata.normalize("NFKD", str(text_value or "").lower())
    normalized = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    normalized = normalized.replace("đ", "d")
    normalized = re.sub(r"[^a-z0-9]+", " ", normalized).strip()
    return normalized in {
        "da", "da a", "da anh hoi di", "da chi hoi di", "da ban hoi di",
        "da minh nghe", "vang", "vang a", "duoc", "duoc a", "ok", "ok a",
        "duoc anh hoi di", "duoc chi hoi di", "okay", "okay a", "roi", "uh", "uhm", "minh san sang",
        "yes", "yes go ahead", "sure", "ready", "okay go ahead",
    }




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


async def _generate_wrap_text(*, purpose: str, context: str, is_vi: bool) -> str:
    """Generate the candidate-facing close; on provider failure, close silently and trace it."""
    language = "Vietnamese" if is_vi else "English"
    try:
        value = await generate_text(
            instructions=(
                f"You are Intervia's interviewer. Write one natural, concise closing line in {language}. "
                "Reflect only the supplied context. Do not mention scores or internal policy. "
                "Return only the line to the candidate."
            ),
            input_text=f"Purpose: {purpose}\nContext: {context}",
            max_output_tokens=100,
        )
        line = " ".join(str(value or "").split())
        if not line:
            raise ValueError("empty_wrap_text")
        trace_event("interviewer", "dialogue_line_generated", purpose=purpose, text=line[:500])
        return line
    except Exception as exc:
        trace_event("interviewer", "dialogue_line_failed", purpose=purpose, error_type=type(exc).__name__)
        return ""


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
        return _speakable_names(full_name, short_name)
    raw_name = await db.scalar(
        text(
            "SELECT NULLIF(BTRIM(u.name), '') FROM interview_sessions s "
            "JOIN users u ON u.id = s.user_id WHERE s.id = :sid"
        ),
        {"sid": session_id},
    )
    return _speakable_names(*_candidate_name_parts(raw_name))


# Vietnamese letters with diacritics; a Vietnamese name written without them
# ("Hien") is read like English by TTS and is ambiguous (Hiền, Hiển, Hiến…).
_VI_DIACRITIC_RE = re.compile(
    "[àáạảãâầấậẩẫăằắặẳẵèéẹẻẽêềếệểễìíịỉĩòóọỏõôồốộổỗơờớợởỡùúụủũưừứựửữỳýỵỷỹđ]", re.IGNORECASE
)
_VI_SURNAMES_ASCII = frozenset(
    "nguyen tran le pham hoang huynh phan vu vo dang bui do ho ngo duong ly dinh truong lam mai ha".split()
)


def _speakable_names(full_name: str | None, short_name: str | None) -> tuple[str | None, str | None]:
    """Avoid reading ambiguous Vietnamese names written without diacritics."""
    if not full_name or _VI_DIACRITIC_RE.search(full_name):
        return full_name, short_name
    words = full_name.split()
    if len(words) == 1 or words[0].casefold() in _VI_SURNAMES_ASCII:
        return None, None
    return full_name, short_name


# Number of recent interviewer lines used to avoid immediate repetition.
RECENT_LINES = 6


def _personalize_opening(text_value: str, full_name: str | None) -> str:
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
    # Where each piece of the turn came from (llm, topic, script, bank…).
    if raw.get("speech_plan"):
        out["speechPlan"] = raw["speech_plan"]
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
            return _personalize_opening(question, short_name)
        return question
    return question


async def _recent_interviewer_lines(
    db: AsyncSession, session_id: str, limit: int = 4, call_name: str | None = None
) -> list[str]:
    """The opening sentence of the interviewer's last few messages, newest last.

    Passed to the core so it neither asks the model for, nor falls back to, a
    line it has just said. Best effort: a read failure only loses variety.
    ``call_name`` is put back to "bạn" so a stored "Ừm, mình hiểu ý Hiền." still
    matches the pooled "Ừm, mình hiểu ý bạn." (including older saved messages).
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
            line = first_paragraph(content)
            if call_name:
                line = re.sub(rf"(?<!\w){re.escape(call_name)}(?!\w)", "bạn", line)
            lines.append(line)
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
    decision_rule: str | None = None,
    flags: list[str] | None = None,
    langfuse_trace_id: str | None = None,
    playbook_ids: list[str] | None = None,
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
        # Speech control (admin "Lời nói AI"): where each part of the reply came
        # from, what the guards did, and terms the AI brought in on its own.
        speech_plan=meta.get("speech_plan") or None,
        speech_checks=speech_checks.collected() or None,
        new_terms=meta.get("new_terms") or None,
        transition=bool(meta.get("transition")),
        qna_reason=meta.get("qna_rejection_reason"),
        # LLM monitoring (application/llm_checks.py, src/core/langfuse.py).
        decision_rule=decision_rule,
        flags=flags or None,
        langfuse_trace_id=langfuse_trace_id,
        # Interviewer playbook entries in the prompts of this turn (helpful / harmful counters).
        playbook_ids=playbook_ids or None,
        # The agent's decision next to the rule's (admin "agent vs rule" column).
        agent_decision=agent_record.summary(meta["decision"]) if isinstance(meta.get("decision"), dict) else None,
    )


def _turn_stage(turn: dict[str, Any]) -> str:
    return str((turn.get("question_snapshot") or {}).get("stage") or "UNKNOWN")


def _accumulate_turn_answer(previous: str, current: str, *, warmup: bool) -> str:
    """Combine answer fragments while keeping warm-up speech readable to the AI."""
    previous = str(previous or "").strip()
    current = str(current or "").strip()
    if not previous:
        return current
    if not current:
        return previous
    if warmup:
        return f"{previous}\n{current}"
    return f"{previous}\n\n[Follow-up Answer]: {current}"


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


async def expire_chat_session_if_due(
    db: AsyncSession, session_row: dict[str, Any], *, has_started: bool,
) -> bool:
    deadline = chat_deadline(session_row, has_started=has_started)
    if session_row["status"] != "OPEN" or deadline is None or datetime.now(UTC) < deadline:
        return False
    await complete_chat_session(db, session_row, reason="HARD_TIMEOUT")
    trace_event(
        "interviewer", "chat_deadline_reached", session_id=str(session_row["id"]),
        deadline=deadline.isoformat(), end_reason=session_row.get("end_reason"),
    )
    return True


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
    if await expire_chat_session_if_due(db, session_row, has_started=bool(messages)):
        return await get_chat_runtime(db, session_row)

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
    # interview_sessions.started_at defaults to the row's creation time; the
    # interview clock really starts with the opening question (start_chat_session
    # resets started_at then). Before that, nothing has been spent.
    has_started = bool(messages)
    if not has_started:
        remaining_sec = target_sec
    deadline = chat_deadline(session_row, has_started=has_started)

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
        "startedAt": (
            session_row.get("started_at").isoformat()
            if has_started and isinstance(session_row.get("started_at"), datetime)
            else None
        ),
        "expiresAt": deadline.isoformat() if deadline else None,
        "serverTime": datetime.now(UTC).isoformat(),
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
    _, candidate_short_name = await _candidate_names(db, session_id)

    # Keep the opening and the frozen first question in separate messages.
    q_snapshot = first_turn.get("question_snapshot") or {}
    q_text = (
        q_snapshot.get("questionText")
        or q_snapshot.get("question_text")
        or warmup_question(is_vi=is_vi, is_demo=False)
    )

    if re.match(r"^\s*(Chào|Hi|Hello)\b", q_text, flags=re.IGNORECASE):
        # A session frozen before the opening script existed: its warm-up
        # snapshot already greets, so only personalise it.
        opening_text = ""
        first_question_text = (
            _personalize_opening(q_text, candidate_short_name) if is_vi else q_text
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
        )
        first_question_text = (
            q_text if is_vi else q_text
        )

    question_sequence = 1
    if opening_text:
        await db.execute(
            text(
                """
                INSERT INTO interview_chat_messages
                (id, session_id, role, content, metadata, turn_id, message_type, sequence, created_at)
                VALUES
                (:id, :sid, 'assistant', :content, CAST(:metadata AS jsonb), :turn_id, 'ACKNOWLEDGMENT', 1, now())
                """
            ),
            {
                "id": str(uuid4()),
                "sid": session_id,
                "content": opening_text,
                "metadata": json.dumps(
                    {"opening": True, "turnIndex": first_turn.get("turn_index", 0), "stage": "WARM_UP"}
                ),
                "turn_id": first_turn["id"],
            },
        )
        question_sequence = 2

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
            (:id, :sid, 'assistant', :content, CAST(:metadata AS jsonb), :turn_id, 'MAIN_QUESTION', :seq, now())
            """
        ),
        {
            "id": question_id,
            "sid": session_id,
            "content": first_question_text,
            "metadata": json.dumps(q_meta),
            "turn_id": first_turn["id"],
            "seq": question_sequence,
        },
    )
    await db.commit()

    return await get_chat_runtime(db, session_row)






# ---------------------------------------------------------------------------
# One candidate turn, in phases (V2 review part 2, brief §8)
# ---------------------------------------------------------------------------
#
# ``process_candidate_message`` used to be one 1,400-line function. It is now
# an orchestrator over five seams, and the transaction shape is unchanged:
#
#   _load_turn_context          reads; raises on an illegal session/turn state
#   _claim_candidate_message    session row lock → idempotency → Phase 1 write → COMMIT
#   _prepare_inference          reads only: session state and the turn input
#   _run_authoritative_inference  the provider call(s), outside any DB lock
#   Phase 3 (in the orchestrator)  re-lock → one domain transition → COMMIT → response
#
# The lock taken by the claim is released by the claim's own commit; the Phase 3
# lock is taken by the orchestrator and released by the commit of whichever
# transition runs. No helper commits on another helper's behalf, and the SQL is
# the same text as before (the tests' fake session matches on it).


@dataclass(frozen=True)
class TurnContext:
    """What is true about the session before this message writes anything."""

    session_id: Any
    session_row: dict[str, Any]
    turns: list[dict[str, Any]]
    current_turn: dict[str, Any]
    last_asst_msg: Any
    cleaned_content: str
    elapsed_override_seconds: float | None

    @property
    def turn_id(self) -> Any:
        return self.current_turn["id"]

    @property
    def turn_index(self) -> Any:
        return self.current_turn["turn_index"]

    @property
    def stage(self) -> str:
        return _turn_stage(self.current_turn)

    @property
    def is_warmup(self) -> bool:
        return self.stage == "WARM_UP"

    @property
    def is_vi(self) -> bool:
        return (self.session_row.get("locale") or "vi").lower().startswith("vi")

    def remaining_seconds(self) -> int:
        return _session_remaining_seconds(
            self.session_row, elapsed_override_seconds=self.elapsed_override_seconds
        )


@dataclass
class MessageClaim:
    """The outcome of Phase 1: the candidate message this turn will answer.

    ``reply`` is set when no inference is needed: the message was already
    answered (replayed from the database) or it was a readiness response that
    released a frozen question. The orchestrator returns it as is.
    """

    user_msg_id: str = ""
    user_seq: int = 0
    asst_seq: int = 0
    previous_answer: Any = None
    telemetry: dict[str, Any] | None = None
    recovered: bool = False
    reply: dict[str, Any] | None = None
    #: None when no preview key was offered; otherwise whether it still matched
    #: the authoritative state under the row lock.
    preview_valid: bool | None = None


@dataclass(frozen=True)
class PreparedTurn:
    """Everything the authoritative inference needs, and nothing it may write."""

    session_state: dict[str, Any]
    turn_input: CandidateTurnInput
    engine: InterviewCoreEngine
    probes_this_turn: int


@dataclass(frozen=True)
class TurnCommit:
    """What Phase 3 commits: the decision and the identifiers it binds to."""

    ctx: TurnContext
    claim: MessageClaim
    prepared: PreparedTurn
    core_output: Any
    asst_msg_id: str
    remaining_time: Any

    @property
    def session_state(self) -> dict[str, Any]:
        return self.prepared.session_state

    @property
    def current_stage(self) -> str:
        return _enum_value(self.core_output.current_stage)

    def candidate_message(self, created_at: str, *, message_type: str = "CANDIDATE_ANSWER") -> dict[str, Any]:
        return _message_ref(
            message_id=self.claim.user_msg_id,
            role="user",
            message_type=message_type,
            turn_id=self.ctx.turn_id,
            sequence=self.claim.user_seq,
            content=self.ctx.cleaned_content,
            created_at=created_at,
        )

    def assistant_message(
        self, created_at: str, *, message_type: str, content: Any, turn_id: Any
    ) -> dict[str, Any]:
        return _message_ref(
            message_id=self.asst_msg_id,
            role="assistant",
            message_type=message_type,
            turn_id=turn_id,
            sequence=self.claim.asst_seq,
            content=content,
            created_at=created_at,
        )


def _enum_value(value: Any) -> str:
    return value.value if hasattr(value, "value") else str(value)


def _message_ref(
    *, message_id: Any, role: str, message_type: str, turn_id: Any, sequence: Any, content: Any, created_at: str
) -> dict[str, Any]:
    return {
        "messageId": message_id,
        "role": role,
        "messageType": message_type,
        "turnId": turn_id,
        "sequence": sequence,
        "content": content,
        "createdAt": created_at,
    }


def _turn_response(
    ctx: TurnContext,
    *,
    stage: Any,
    turn_index: Any,
    action: Any,
    message: Any,
    remaining_time: Any,
    probe_count: int,
    completed: bool,
    user: dict[str, Any],
    assistant: dict[str, Any],
    turn_status: dict[str, Any],
    session_status: Any,
    **extra: Any,
) -> dict[str, Any]:
    """The one response shape every turn outcome is reported in."""
    return {
        "sessionId": ctx.session_id,
        "currentStage": stage,
        "currentTurnIndex": turn_index,
        "action": action,
        "message": message,
        "remainingTimeSeconds": remaining_time,
        "probeCount": probe_count,
        "completed": completed,
        "userMessage": user,
        "assistantResponse": assistant,
        "turnStatus": turn_status,
        "sessionStatus": session_status,
        **extra,
    }


def _turn_status(turn_id: Any, turn_index: Any, *, is_follow_up: bool, completed: bool) -> dict[str, Any]:
    return {"turnId": turn_id, "turnIndex": turn_index, "isFollowUp": is_follow_up, "completed": completed}


def _parse_metadata(raw: Any) -> dict[str, Any]:
    """A jsonb column as a dict, whatever the driver handed back."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return {}
    return raw if isinstance(raw, dict) else {}


def _decision_trace_fields(core_output: Any, session_state: dict[str, Any]) -> dict[str, Any]:
    """The decision provenance every ``stage_transition_committed`` event carries."""
    decision_payload = (core_output.metadata or {}).get("decision") or {}
    selected_action = decision_payload.get(decision_payload.get("executed") or "rule") or {}
    contract_context = session_state.get("interview_contract") or {}
    return {
        "selected_action": selected_action.get("action"),
        "agent_action": (decision_payload.get("agent") or {}).get("action"),
        "rule_action": (decision_payload.get("rule") or {}).get("action"),
        "output_action": _enum_value(core_output.action),
        "decision_source": decision_payload.get("executed"),
        "transition_reason": (decision_payload.get("transition") or {}).get("reason"),
        "contract_reference": contract_context.get("reference"),
        "session_plan_fingerprint": contract_context.get("session_plan_fingerprint"),
        "coverage_target": contract_context.get("coverage_target"),
    }


# ------------------------------------------------------------ context


_TURNS_SELECT = """
            SELECT id, turn_index, status, question_version_id, question_snapshot, answer_text, started_at
            FROM interview_turns
            WHERE session_id = :session_id
            ORDER BY turn_index ASC
            """


async def _load_turns(db: AsyncSession, session_id: Any) -> list[dict[str, Any]]:
    turns_res = await db.execute(text(_TURNS_SELECT), {"session_id": session_id})
    return [dict(r) for r in turns_res.mappings().all()]


async def _load_turn_context(
    db: AsyncSession,
    session_row: dict[str, Any],
    content: str,
    *,
    elapsed_override_seconds: float | None,
) -> TurnContext:
    """Read the session and its turns; refuse anything the turn contract forbids."""
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
    turns = await _load_turns(db, session_id)
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
            SELECT id, turn_id, message_type, sequence, content, metadata
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

    if await expire_chat_session_if_due(db, session_row, has_started=bool(last_asst_msg)):
        raise ChatRuntimeError("SESSION_CLOSED: Thời gian phỏng vấn đã hết.")

    return TurnContext(
        session_id=session_id,
        session_row=session_row,
        turns=turns,
        current_turn=current_turn,
        last_asst_msg=last_asst_msg,
        cleaned_content=cleaned_content,
        elapsed_override_seconds=elapsed_override_seconds,
    )


# ------------------------------------------------------------ Phase 1


async def _claim_candidate_message(
    db: AsyncSession,
    ctx: TurnContext,
    *,
    client_message_id: str | None,
    telemetry: dict[str, Any] | None,
    preview_key: dict[str, Any] | None = None,
) -> MessageClaim:
    """Lock the session, settle idempotency, write the candidate message, commit.

    Owns exactly one transaction: the ``FOR UPDATE`` taken here is released by
    the commit at the end of whichever branch runs, before any provider call.
    """
    session_id = ctx.session_id
    current_turn = ctx.current_turn
    current_turn_id = ctx.turn_id
    current_turn_index = ctx.turn_index
    cleaned_content = ctx.cleaned_content
    claim = MessageClaim(previous_answer=current_turn.get("answer_text"), telemetry=telemetry)

    # Serialize messages per session. Phase 1 commits before the LLM call, so a
    # second message arriving meanwhile used to take the sequence reserved for
    # the first reply (IntegrityError on its Phase 3, and a retry could return
    # the *candidate's* next message as the assistant reply). The row lock makes
    # the in-flight check and the Phase 1 insert atomic.
    await db.execute(
        text("SELECT id FROM interview_sessions WHERE id = :sid FOR UPDATE"),
        {"sid": session_id},
    )

    # The context was read before this lock. A turn that moved meanwhile -- a
    # concurrent message committing its Phase 3, a replan, the reaper -- must
    # not receive this answer: re-read under the lock and refuse if the ASKED
    # turn is no longer the one the context was built from.
    turns_now = await _load_turns(db, session_id)
    asked_now = [t for t in turns_now if t.get("status") == "ASKED"]
    if len(asked_now) != 1 or str(asked_now[0]["id"]) != str(current_turn_id):
        raise ChatRuntimeError(
            "INVALID_TURN_STATE: Lượt câu hỏi đã thay đổi trong lúc xử lý. Vui lòng gửi lại câu trả lời."
        )
    if preview_key is not None:
        # V2 spec §7: the preview was validated at redemption, but that was
        # another transaction. Rebuild the authoritative key from the rows
        # read under this lock and compare strictly; a moved field means the
        # decision belongs to a turn that no longer exists as it was.
        final_key = authoritative_preview_key(
            session=ctx.session_row, turns=turns_now, transcript=cleaned_content
        )
        rejection = validate_preview(
            preview=preview_key, computed_for=PreviewKey.from_dict(preview_key), final=final_key, strict=True
        )
        claim.preview_valid = rejection is None
        if rejection is not None:
            trace_event(
                "interviewer", "preview_rejected_at_claim",
                session_id=session_id, turn_id=current_turn_id,
                reason=rejection.reason, fields=",".join(rejection.fields),
            )

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
                metadata = _parse_metadata(existing_user_msg.get("metadata") or {})
                processing_failed = metadata.get("processingStatus") == "FAILED"
                age_seconds = existing_user_msg.get("age_seconds")
                created_at = existing_user_msg.get("created_at")
                if age_seconds is None and isinstance(created_at, datetime):
                    if created_at.tzinfo is None:
                        created_at = created_at.replace(tzinfo=UTC)
                    age_seconds = (datetime.now(UTC) - created_at).total_seconds()
                if not processing_failed and float(age_seconds or 0) < _IN_FLIGHT_STALE_SECONDS:
                    raise ChatRuntimeError(
                        "IN_PROGRESS: Câu trả lời trước vẫn đang được xử lý. Vui lòng chờ phản hồi của người phỏng vấn."
                    )
                if str(existing_user_msg.get("content") or "").strip() != cleaned_content:
                    raise ValueError("EVENT_ID_PAYLOAD_MISMATCH")
                claim.previous_answer = metadata.get("previousAnswer")
                claim.telemetry = metadata.get("telemetry") or telemetry
                claim.user_msg_id = str(existing_user_msg["id"])
                claim.user_seq = int(existing_user_msg["sequence"])
                claim.asst_seq = claim.user_seq + 1
                claim.recovered = True
            else:
                claim.reply = {
                    "sessionId": session_id,
                    "currentStage": (current_turn.get("question_snapshot") or {}).get("stage", "INTRO"),
                    "currentTurnIndex": current_turn_index,
                    "action": asst_msg.get("message_type"),
                    "message": asst_msg.get("content") or "",
                    "remainingTimeSeconds": ctx.remaining_seconds(),
                    "probeCount": 0,
                    "completed": False,
                    "userMessage": _message_payload(dict(existing_user_msg)),
                    "assistantResponse": _message_payload(dict(asst_msg)),
                    "turnStatus": {"completed": False},
                    "sessionStatus": ctx.session_row["status"],
                }
                return claim

    if not claim.recovered:
        last_msg = (
            await db.execute(
                text(
                    """
                    SELECT id, session_id, role, content, metadata, created_at,
                           turn_id, message_type, sequence, client_message_id,
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
            metadata = _parse_metadata(last_msg.get("metadata") or {})
            processing_failed = metadata.get("processingStatus") == "FAILED"
            if not processing_failed and float(last_msg.get("age_seconds") or 0) < _IN_FLIGHT_STALE_SECONDS:
                raise ChatRuntimeError(
                    "IN_PROGRESS: Câu trả lời trước vẫn đang được xử lý. Vui lòng chờ phản hồi của người phỏng vấn."
                )
            if str(last_msg.get("content") or "").strip() != cleaned_content:
                raise ChatRuntimeError(
                    "RECOVERY_REQUIRED: Câu trả lời trước đã được lưu nhưng chưa có phản hồi. "
                    "Vui lòng gửi lại đúng tin nhắn đó trước khi trả lời câu mới."
                )

            # A browser refresh or an older client may retry the exact saved
            # answer with a new/missing event id. Recover the persisted phase-1
            # row by content instead of leaving this session permanently stuck.
            claim.previous_answer = metadata.get("previousAnswer")
            claim.telemetry = metadata.get("telemetry") or telemetry
            claim.user_msg_id = str(last_msg["id"])
            claim.user_seq = int(last_msg["sequence"])
            claim.asst_seq = claim.user_seq + 1
            claim.recovered = True

    # A stage transition is a separate conversational beat. A short "dạ" only
    # confirms readiness; it must not be evaluated or stored as the answer to
    # the question that follows.
    transition_metadata = _parse_metadata((ctx.last_asst_msg or {}).get("metadata") or {})
    if (
        not claim.recovered
        and transition_metadata.get("awaitingCandidateAck") is True
        and _is_simple_transition_ack(cleaned_content)
    ):
        claim.reply = await _release_frozen_question(db, ctx, client_message_id=client_message_id)
        return claim

    # =========================================================================
    # PHASE 1: Fast Write Candidate Message (Committed immediately)
    # =========================================================================
    if not claim.recovered:
        max_seq = await db.scalar(
            text("SELECT COALESCE(MAX(sequence), 0) FROM interview_chat_messages WHERE session_id = :sid"),
            {"sid": session_id},
        )
        claim.user_seq = int(max_seq or 0) + 1
        claim.asst_seq = claim.user_seq + 1
        claim.user_msg_id = str(uuid4())
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
                    "id": claim.user_msg_id,
                    "sid": session_id,
                    "content": cleaned_content,
                    "metadata": json.dumps(
                        {
                            "telemetry": telemetry or {},
                            "previousAnswer": claim.previous_answer,
                            "processingStatus": "PROCESSING",
                        }
                    ),
                    "turn_id": current_turn_id,
                    "seq": claim.user_seq,
                    "cid": client_message_id,
                },
            )
        except IntegrityError:
            await db.rollback()
            raise ChatRuntimeError(
                "IN_PROGRESS: Câu trả lời đang được xử lý. Vui lòng thử lại sau."
            )

        existing_answer = claim.previous_answer or ""
        new_accumulated = _accumulate_turn_answer(
            existing_answer, cleaned_content, warmup=ctx.is_warmup
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
    if claim.recovered:
        await db.execute(
            text(
                """
                UPDATE interview_chat_messages
                SET metadata = jsonb_set(
                    COALESCE(metadata, '{}'::jsonb),
                    '{processingStatus}', '"PROCESSING"'::jsonb, true
                )
                WHERE id = :message_id
                """
            ),
            {"message_id": claim.user_msg_id},
        )
    await db.commit()
    return claim


async def _release_frozen_question(
    db: AsyncSession, ctx: TurnContext, *, client_message_id: str | None
) -> dict[str, Any]:
    """A readiness response after a topic signpost: say the frozen question, no Core call."""
    session_id = ctx.session_id
    current_turn_id = ctx.turn_id
    current_turn_index = ctx.turn_index
    cleaned_content = ctx.cleaned_content
    snap = ctx.current_turn.get("question_snapshot") or {}
    next_question_text = (
        snap.get("spokenText")
        or snap.get("questionText")
        or snap.get("question_text")
        or ("Câu hỏi tiếp theo dành cho bạn." if (ctx.session_row.get("locale") or "vi").lower().startswith("vi") else "Here is your next question.")
    )
    max_seq = await db.scalar(
        text("SELECT COALESCE(MAX(sequence), 0) FROM interview_chat_messages WHERE session_id = :sid"),
        {"sid": session_id},
    )
    ack_seq = int(max_seq or 0) + 1
    question_seq = ack_seq + 1
    ack_id = str(uuid4())
    question_message_id = str(uuid4())
    stage = str(snap.get("stage") or "")
    question_meta = {
        "turnIndex": current_turn_index,
        "transitionAckConsumed": True,
    }
    if stage:
        question_meta["stage"] = stage
    if snap.get("projectName"):
        question_meta["project_name"] = snap["projectName"]
    if snap.get("projectId"):
        question_meta["project_id"] = snap["projectId"]
    await db.execute(
        text(
            """
            INSERT INTO interview_chat_messages
            (id, session_id, role, content, metadata, turn_id, message_type, sequence, client_message_id, created_at)
            VALUES (:id, :sid, 'user', :content, CAST(:metadata AS jsonb), :turn_id, 'ACKNOWLEDGMENT', :seq, :cid, now())
            """
        ),
        {
            "id": ack_id,
            "sid": session_id,
            "content": cleaned_content,
            "metadata": json.dumps({"purpose": "transition_ready"}),
            "turn_id": current_turn_id,
            "seq": ack_seq,
            "cid": client_message_id,
        },
    )
    await db.execute(
        text(
            """
            INSERT INTO interview_chat_messages
            (id, session_id, role, content, metadata, turn_id, message_type, sequence, created_at)
            VALUES (:id, :sid, 'assistant', :content, CAST(:metadata AS jsonb), :turn_id, 'MAIN_QUESTION', :seq, now())
            """
        ),
        {
            "id": question_message_id,
            "sid": session_id,
            "content": next_question_text,
            "metadata": json.dumps(question_meta),
            "turn_id": current_turn_id,
            "seq": question_seq,
        },
    )
    await db.commit()
    now_iso = datetime.now(UTC).isoformat()
    return _turn_response(
        ctx,
        stage=stage or "VALIDATE",
        turn_index=current_turn_index,
        action="ASK_MAIN",
        message=next_question_text,
        remaining_time=ctx.remaining_seconds(),
        probe_count=0,
        completed=False,
        user=_message_ref(
            message_id=ack_id, role="user", message_type="ACKNOWLEDGMENT", turn_id=current_turn_id,
            sequence=ack_seq, content=cleaned_content, created_at=now_iso,
        ),
        assistant=_message_ref(
            message_id=question_message_id, role="assistant", message_type="MAIN_QUESTION",
            turn_id=current_turn_id, sequence=question_seq, content=next_question_text, created_at=now_iso,
        ),
        turn_status=_turn_status(current_turn_id, current_turn_index, is_follow_up=False, completed=False),
        session_status="OPEN",
    )


# ------------------------------------------------------------ Phase 2


async def _prepare_inference(
    db: AsyncSession,
    ctx: TurnContext,
    claim: MessageClaim,
    *,
    modality: str,
    duration_seconds: float,
    precomputed_evaluation: dict[str, Any] | None,
    precomputed_decision: dict[str, Any] | None,
    core_engine: InterviewCoreEngine | None,
) -> PreparedTurn:
    """Build the session state and the turn input. Reads only; outside any lock."""
    session_id = ctx.session_id
    session_row = ctx.session_row
    current_turn = ctx.current_turn
    current_turn_id = ctx.turn_id
    turns = ctx.turns

    job_title = await _get_job_title(db, session_row.get("job_id"))
    candidate_full_name, candidate_short_name = await _candidate_names(db, session_id)
    # Masked out of every payload sent to Langfuse.
    langfuse.register_names(candidate_full_name, candidate_short_name)

    # Count all previous probes and clarifies in session and current turn
    probe_msgs_res = await db.execute(
        text(
            """
            SELECT turn_id, message_type, content
            FROM interview_chat_messages
            WHERE session_id = :sid AND role = 'assistant' AND message_type IN ('PROBE', 'CLARIFY')
              -- Repeating an unheard question or answering a question about the
              -- interview is not a candidate answer to the active prompt.
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
    validate_turn_ids = {t["id"] for t in turns if (t.get("question_snapshot") or {}).get("stage") == "VALIDATE"}
    cv_followup_used = 1 if any(m["turn_id"] in validate_turn_ids and m["message_type"] == "PROBE" for m in all_probe_msgs) else 0

    # Engine events without a session id (guardrail fallbacks, model calls)
    # belong to this interview in the admin pipeline view.
    current_session_id.set(str(session_id))
    # Call Modality-Agnostic Core Engine
    session_state = build_session_state_from_db(
        session_row,
        turns,
        current_turn,
        curr_turn_probes,
        session_probe_count=session_probe_count,
        cv_followup_used=cv_followup_used,
        clarify_count=curr_turn_clarifies,
        elapsed_override_seconds=ctx.elapsed_override_seconds,
    )
    candidate_tone = str(
        (session_state.get("candidate_settings") or {}).get("aiTone") or "standard"
    )
    engine = core_engine or InterviewCoreEngine(
        ai_generator=generate_text,
        candidate_tone=candidate_tone,
    )
    locked_plan_result = await db.execute(
        text("SELECT plan_payload FROM interview_session_plans WHERE session_id = :sid"),
        {"sid": session_id},
    )
    locked_plan_payload = locked_plan_result.scalar_one_or_none() or {}
    if isinstance(locked_plan_payload, str):
        try:
            locked_plan_payload = json.loads(locked_plan_payload)
        except (TypeError, ValueError):
            locked_plan_payload = {}
    if isinstance(locked_plan_payload, dict):
        session_state["contract_targets"] = list(locked_plan_payload.get("targets") or [])
        session_state["interview_contract"]["session_plan_fingerprint"] = str(
            locked_plan_payload.get("fingerprint") or ""
        )
    session_state["job_title"] = job_title
    current_turn_probe_texts = [
        m.get("content") for m in all_probe_msgs
        if m["turn_id"] == current_turn_id and m["message_type"] == "PROBE" and m.get("content")
    ]
    if current_turn_probe_texts and session_state.get("current_question_context"):
        # Probe messages now contain one complete spoken follow-up line.
        session_state["current_question_context"]["follow_up_prompt"] = (
            last_paragraph(current_turn_probe_texts[-1]) or current_turn_probe_texts[-1]
        )
    session_state["recent_interviewer_lines"] = await _recent_interviewer_lines(
        db, session_id, limit=RECENT_LINES, call_name=candidate_short_name
    )
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
        turn_index=ctx.turn_index,
        # The warm-up is one answer assembled from several chat messages.
        # Keep the chat transcript messages separate, but let the interviewer
        # evaluate the full self-introduction rather than forgetting earlier
        # details after every short continuation.
        text_content=(
            _accumulate_turn_answer(claim.previous_answer or "", ctx.cleaned_content, warmup=True)
            if ctx.is_warmup
            else ctx.cleaned_content
        ),
        modality=modality,
        duration_seconds=duration_seconds,
        telemetry=claim.telemetry or {},
        precomputed_evaluation=precomputed_evaluation,
        precomputed_decision=precomputed_decision,
    )
    return PreparedTurn(
        session_state=session_state,
        turn_input=turn_input,
        engine=engine,
        probes_this_turn=curr_turn_probes,
    )


async def _run_authoritative_inference(
    db: AsyncSession,
    ctx: TurnContext,
    prepared: PreparedTurn,
    *,
    modality: str,
    on_early_speech: Any,
) -> Any:
    """The canonical decision for this turn: the agent's turn graph, traced.

    The only place in the runtime a provider is called for the turn. It holds
    no database lock; the row lock was released by the claim's commit.
    """
    session_id = ctx.session_id
    session_row = ctx.session_row
    session_state = prepared.session_state
    cleaned_content = ctx.cleaned_content
    current_turn_id = ctx.turn_id
    current_turn_index = ctx.turn_index

    # Langfuse: one trace per turn with every model call, the decision and the
    # rule checks (src/core/langfuse.py). A no-op when Langfuse is not configured.
    question_ctx = session_state.get("current_question_context") or {}
    question_text = str(question_ctx.get("follow_up_prompt") or question_ctx.get("main_prompt") or "")
    # Approved lessons from past interviews (application/interviewer_playbook.py), cached 60 s.
    await interviewer_playbook.refresh(db)
    lf_stack = ExitStack()
    lf_turn = lf_stack.enter_context(
        langfuse.trace(
            langfuse.TURN_TRACE,
            session_id=str(session_id),
            user_id=str(session_row.get("user_id") or "") or None,
            input={
                "question": question_text,
                "stage": session_state.get("current_stage"),
                "modality": modality,
            },
            private_input={"answer": cleaned_content},
            metadata={
                "turn_index": str(current_turn_index),
                "modality": str(modality or "CHAT"),
                "playbook": playbook.version() or "none",
            },
        )
    )
    try:
        core_started = perf_counter()
        # Every guard decision on the AI's lines in this turn (core/dialogue/speech_checks.py).
        speech_checks.begin()
        try:
            # The agent's turn graph: guards_in → menu → decide → act → guards_out → record.
            # Same pipeline as engine.handle_turn, plus the menu, the pacing block and
            # the decision record (INTERVIEW_AGENT_DECISION_PLAN_V1 Pha 1).
            memory_search = None
            if candidate_memory.enabled() != "0":
                # Candidate memory (plan §7): topics from earlier sessions for the prompts,
                # and the decider's search tool. Never on the critical path: failures → no memory.
                memory_user = str(session_row.get("user_id") or "")
                session_state["candidate_memory"] = await candidate_memory.load(memory_user)
                memory_search = candidate_memory.search_for(memory_user)
            core_output = await run_turn(
                prepared.engine, prepared.turn_input, session_state,
                memory_search=memory_search, on_early_speech=on_early_speech,
            )
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

        core_ms = round((perf_counter() - core_started) * 1000)
        turn_meta = core_output.metadata or {}
        action_value = _enum_value(core_output.action)
        rule = llm_checks.decision_rule(
            action_value, {**turn_meta, "exit_reason": getattr(core_output.exit_reason, "value", None)}
        )
        checks = speech_checks.collected()
        flags = llm_checks.turn_flags(
            action=action_value,
            meta=turn_meta,
            answer=cleaned_content,
            question=question_text,
            rubric=str(question_ctx.get("rubric_criteria") or ""),
            reply=str(core_output.message_text or ""),
            checks=checks,
        )
        _trace_spoken_turn(
            session_id=session_id,
            turn_index=current_turn_index,
            candidate_text=cleaned_content,
            modality=modality,
            core_output=core_output,
            recent_lines=session_state.get("recent_interviewer_lines") or [],
            core_ms=core_ms,
            decision_rule=rule,
            flags=flags,
            langfuse_trace_id=lf_turn.trace_id if lf_turn else None,
            playbook_ids=playbook.ids(),
        )
        langfuse.finish(
            lf_turn,
            output={
                "action": action_value,
                "decision_rule": rule,
                "intent": turn_meta.get("intent"),
                "sufficiency": turn_meta.get("sufficiency_status"),
                "missing_aspect": turn_meta.get("missing_aspect"),
                "reply": core_output.message_text,
                "speech_plan": turn_meta.get("speech_plan"),
                "speech_checks": checks,
                "new_terms": turn_meta.get("new_terms"),
            },
            flags=flags,
            private_input={"question": question_text, "answer": cleaned_content},
            scores={
                "guard.rejects": sum(1 for c in checks if c.get("outcome") == "reject"),
                # agent.* (INTERVIEW_AGENT_DECISION_PLAN_V1 §5.3): agreement, violations, who executed.
                **agent_record.scores(turn_meta["decision"] if isinstance(turn_meta.get("decision"), dict) else {}),
            },
        )
    finally:
        lf_stack.close()

    trace_event(
        "interviewer",
        "turn_action_dispatched",
        session_id=session_id,
        turn_id=current_turn_id,
        turn_index=current_turn_index,
        action=_enum_value(core_output.action),
        current_stage=_enum_value(core_output.current_stage),
        is_session_finished=core_output.is_session_finished,
    )
    return core_output


async def _persist_turn_counters(
    db: AsyncSession, ctx: TurnContext, core_output: Any, session_state: dict[str, Any]
) -> None:
    """Persist the canonical counter and its deprecated compatibility alias."""
    session_row = ctx.session_row
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
        {"meta": json.dumps(meta), "sid": ctx.session_id},
    )


# ------------------------------------------------------------ Phase 3


async def _commit_abort(db: AsyncSession, commit: TurnCommit) -> dict[str, Any]:
    """CONFIRM_ABORT: a stop request is not an answer; ask the candidate to confirm."""
    ctx, claim, out = commit.ctx, commit.claim, commit.core_output
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
        {"previous_answer": claim.previous_answer, "turn_id": ctx.turn_id},
    )
    await db.execute(
        text(
            """
            UPDATE interview_chat_messages
            SET metadata = metadata || '{"abortRequest": true}'::jsonb
            WHERE id = :message_id
            """
        ),
        {"message_id": claim.user_msg_id},
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
            "id": commit.asst_msg_id,
            "sid": ctx.session_id,
            "content": out.message_text,
            "turn_id": ctx.turn_id,
            "seq": claim.asst_seq,
        },
    )
    await db.commit()
    now_iso = datetime.now(UTC).isoformat()
    return _turn_response(
        ctx,
        stage=commit.current_stage,
        turn_index=ctx.turn_index,
        action="CONFIRM_ABORT",
        message=out.message_text,
        remaining_time=commit.remaining_time,
        probe_count=commit.prepared.probes_this_turn,
        completed=False,
        user=commit.candidate_message(now_iso),
        assistant=commit.assistant_message(
            now_iso, message_type="CONFIRM_ABORT", content=out.message_text, turn_id=ctx.turn_id
        ),
        turn_status=_turn_status(ctx.turn_id, ctx.turn_index, is_follow_up=False, completed=False),
        session_status="OPEN",
    )


async def _commit_clarify(db: AsyncSession, commit: TurnCommit) -> dict[str, Any]:
    """CLARIFY: answer a question about the interview; the turn stays open."""
    ctx, claim, out = commit.ctx, commit.claim, commit.core_output
    candidate_question_intent = out.metadata.get("candidate_question_intent")
    repeat_request = bool(out.metadata.get("repeat_request"))
    warmup_continuation = bool(out.metadata.get("warmup_continuation"))
    user_message_type = "CANDIDATE_QUESTION" if candidate_question_intent else "CLARIFY"
    # A request directed at the interviewer is not assessment evidence.
    # Warm-up continuations are different: those messages are fragments of
    # one self-introduction, so retain the combined answer for the next
    # attempt. Other clarification messages restore the previous answer.
    await db.execute(
        text(
            """
            UPDATE interview_turns
            SET answer_text = :previous_answer, updated_at = now()
            WHERE id = :turn_id
            """
        ),
        {
            "previous_answer": (
                commit.prepared.turn_input.text_content if warmup_continuation else claim.previous_answer
            ),
            "turn_id": ctx.turn_id,
        },
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
                else {"warmupContinuation": True}
                if warmup_continuation
                else {"clarificationRequest": True}
            ),
            "message_id": claim.user_msg_id,
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
            "id": commit.asst_msg_id,
            "sid": ctx.session_id,
            "content": out.message_text,
            "metadata": json.dumps(
                (
                    {"candidateQuestionIntent": candidate_question_intent}
                    if candidate_question_intent
                    else {"repeatRequest": True}
                    if repeat_request
                    else {"warmupContinuation": True}
                    if warmup_continuation
                    else {}
                )
                | _provenance_metadata(out)
            ),
            "turn_id": ctx.turn_id,
            "msg_type": "CLARIFY",
            "seq": claim.asst_seq,
        },
    )
    await db.commit()
    now_iso = datetime.now(UTC).isoformat()
    return _turn_response(
        ctx,
        stage=commit.current_stage,
        turn_index=ctx.turn_index,
        action="CLARIFY",
        message=out.message_text,
        remaining_time=commit.remaining_time,
        probe_count=commit.prepared.probes_this_turn,
        completed=False,
        user=commit.candidate_message(now_iso, message_type=user_message_type),
        assistant=commit.assistant_message(
            now_iso, message_type="CLARIFY", content=out.message_text, turn_id=ctx.turn_id
        ),
        turn_status=_turn_status(ctx.turn_id, ctx.turn_index, is_follow_up=True, completed=False),
        session_status="OPEN",
    )


async def _commit_probe(db: AsyncSession, commit: TurnCommit) -> dict[str, Any]:
    """PROBE: one follow-up on the same question."""
    ctx, claim, out = commit.ctx, commit.claim, commit.core_output
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
            "id": commit.asst_msg_id,
            "sid": ctx.session_id,
            "content": out.message_text,
            "metadata": json.dumps(_provenance_metadata(out)),
            "turn_id": ctx.turn_id,
            "msg_type": "PROBE",
            "seq": claim.asst_seq,
        },
    )
    await db.commit()
    now_iso = datetime.now(UTC).isoformat()
    return _turn_response(
        ctx,
        stage=commit.current_stage,
        turn_index=ctx.turn_index,
        action="PROBE",
        message=out.message_text,
        remaining_time=commit.remaining_time,
        probe_count=commit.prepared.probes_this_turn + 1,
        completed=False,
        user=commit.candidate_message(now_iso),
        assistant=commit.assistant_message(
            now_iso, message_type="PROBE", content=out.message_text, turn_id=ctx.turn_id
        ),
        turn_status=_turn_status(ctx.turn_id, ctx.turn_index, is_follow_up=True, completed=False),
        session_status="OPEN",
    )


async def _commit_closing_reply(db: AsyncSession, commit: TurnCommit) -> dict[str, Any]:
    """Candidate asked a question in Q&A stage: reply directly within the CLOSING turn."""
    ctx, claim, out = commit.ctx, commit.claim, commit.core_output
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
            "id": commit.asst_msg_id,
            "sid": ctx.session_id,
            "content": out.message_text,
            # Typed MAIN_QUESTION for the transcript, but it is the model's
            # answer to the candidate, never a bank question.
            "metadata": json.dumps({"is_qna_answer": True} | _provenance_metadata(out)),
            "turn_id": ctx.turn_id,
            "seq": claim.asst_seq,
        },
    )
    await db.commit()
    now_iso = datetime.now(UTC).isoformat()
    return _turn_response(
        ctx,
        stage="CLOSING",
        turn_index=ctx.turn_index,
        action="ASK_CLOSING",
        message=out.message_text,
        remaining_time=commit.remaining_time,
        probe_count=commit.prepared.probes_this_turn,
        completed=False,
        user=commit.candidate_message(now_iso),
        assistant=commit.assistant_message(
            now_iso, message_type="MAIN_QUESTION", content=out.message_text, turn_id=ctx.turn_id
        ),
        turn_status=_turn_status(ctx.turn_id, ctx.turn_index, is_follow_up=True, completed=False),
        session_status="OPEN",
    )


async def _settle_current_turn(db: AsyncSession, commit: TurnCommit) -> dict[str, Any] | None:
    """Mark the current turn answered and choose what comes next (not yet started).

    Returns the next PLANNED turn, a CLOSING turn created on the spot when the
    engine entered CLOSING without one, or ``None`` when nothing is left.
    Technical turns the behavioral reserve jump leaves behind are settled as
    SKIPPED here so the BEHAVIORAL invariant holds on the next message.
    """
    ctx, out = commit.ctx, commit.core_output
    turns = ctx.turns
    current_turn_id = ctx.turn_id

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

    target_qid = out.metadata.get("question_id")
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
    if not next_turn and out.current_stage == InterviewStage.CLOSING and not out.is_session_finished:
        closing_turn_id = str(uuid4())
        # Turn order is not monotonic (the reserve jump asks BEHAVIORAL, the
        # highest index, before lower-index turns), so current+1 can collide
        # with uq_interview_turn_order.
        closing_turn_index = max(t["turn_index"] for t in turns) + 1
        closing_snap = {
            "stage": "CLOSING",
            "questionSource": "closing_template",
            "questionText": out.message_text,
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
                "sid": ctx.session_id,
                "tidx": closing_turn_index,
                "snap": json.dumps(closing_snap),
            },
        )
        next_turn = {
            "id": closing_turn_id,
            "session_id": ctx.session_id,
            "turn_index": closing_turn_index,
            "status": "ASKED",
            "question_snapshot": closing_snap,
            "question_version_id": None,
        }
        turns.append(next_turn)

    next_stage = (next_turn.get("question_snapshot") or {}).get("stage") if next_turn else None
    if next_stage == "BEHAVIORAL" and not out.is_session_finished:
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
    return next_turn


async def _ask_next_question(
    db: AsyncSession, commit: TurnCommit, next_turn: dict[str, Any]
) -> dict[str, Any]:
    """Start ``next_turn`` and say its question (or the topic signpost first)."""
    ctx, claim, out = commit.ctx, commit.claim, commit.core_output
    is_vi = ctx.is_vi
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
    awaiting_candidate_ack = bool(
        (out.metadata or {}).get("awaiting_candidate_ack")
    )
    if awaiting_candidate_ack:
        # Send only the topic signpost now. The frozen question follows
        # after a short readiness response, handled without invoking Core.
        next_content = out.message_text or generic_transition(
            (out.metadata or {}).get("transition_topic"), is_vi
        )
    else:
        # Send one complete utterance from Core, or the frozen question.
        # Never append the bank question to an unrelated generated phrase.
        next_content = out.message_text or next_q_text
        if next_q_text not in next_content:
            next_content = next_q_text
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
    assistant_message_type = "MAIN_QUESTION"
    if awaiting_candidate_ack:
        assistant_message_type = "ACKNOWLEDGMENT"
        q_meta["awaitingCandidateAck"] = True
        q_meta["transitionTopic"] = (out.metadata or {}).get("transition_topic")
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
            "id": commit.asst_msg_id,
            "sid": ctx.session_id,
            "content": next_content,
            "metadata": json.dumps(q_meta),
            "turn_id": next_turn["id"],
            "msg_type": assistant_message_type,
            "seq": claim.asst_seq,
        },
    )
    await db.commit()
    next_stage_val = (next_turn.get("question_snapshot") or {}).get("stage") or commit.current_stage
    trace_event(
        "interviewer",
        "stage_transition_committed",
        session_id=ctx.session_id,
        turn_id=ctx.turn_id,
        turn_index=ctx.turn_index,
        from_stage=ctx.stage,
        to_stage=next_stage_val,
        next_turn_id=next_turn["id"],
        next_turn_index=next_turn["turn_index"],
        **_decision_trace_fields(out, commit.session_state),
    )
    now_iso = datetime.now(UTC).isoformat()
    return _turn_response(
        ctx,
        stage=next_stage_val,
        turn_index=next_turn["turn_index"],
        action="ASK_MAIN",
        message=next_content,
        remaining_time=commit.remaining_time,
        probe_count=0,
        completed=False,
        user=commit.candidate_message(now_iso),
        assistant=commit.assistant_message(
            now_iso, message_type=assistant_message_type, content=next_content, turn_id=next_turn["id"]
        ),
        turn_status=_turn_status(next_turn["id"], next_turn["turn_index"], is_follow_up=False, completed=False),
        session_status="OPEN",
    )


async def _close_session(db: AsyncSession, commit: TurnCommit) -> dict[str, Any]:
    """All turns finished or terminated: wrap up, close the session, commit."""
    ctx, claim, out = commit.ctx, commit.claim, commit.core_output
    session_state = commit.session_state
    remaining_time = commit.remaining_time
    turns = ctx.turns

    end_reason: EndReason = "COMPLETED"
    if out.exit_reason:
        if out.exit_reason == SessionExitReason.CANDIDATE_ABORT:
            end_reason = "USER_ENDED"
        elif out.exit_reason == SessionExitReason.HARD_TIMEOUT:
            end_reason = "HARD_TIMEOUT"
        elif out.exit_reason == SessionExitReason.FAST_FAIL_TECH:
            end_reason = "FAST_FAIL_TECH"
        else:
            end_reason = "COMPLETED"

    downgraded_from_completed = False
    ran_out_of_time = False
    unfinished: list[dict[str, Any]] = []
    if end_reason == "COMPLETED":
        # The engine owns the decision to stop; only the label can be wrong here.
        # The <= 90s pacing cutoff ends a session while frozen turns are still
        # PLANNED, so calling that "COMPLETED" would misreport coverage.
        #
        # Downgrade the reason instead of raising. Phase 1 has already committed
        # the candidate message, so aborting at this point used to leave the turn
        # ASKED with no assistant reply, and every retry failed the same way with
        # no way out of the room except the manual end button.
        unfinished = _unfinished_agenda_turns(turns, current_turn_id=ctx.turn_id)
        if unfinished:
            downgraded_from_completed = True
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
                session_id=ctx.session_id,
                turn_id=ctx.turn_id,
                downgraded_to=end_reason,
                remaining_time_seconds=remaining_time,
                reserve_seconds=reserve_seconds,
                unfinished_turn_count=len(unfinished),
                unfinished_stages=sorted({_turn_stage(turn) for turn in unfinished}),
            )

    wrap_text = "" if downgraded_from_completed else (
        out.message_text if out.is_session_finished else ""
    )
    if not wrap_text:
        purpose = "partial_timeout" if downgraded_from_completed and ran_out_of_time else (
            "partial_technical_stop" if downgraded_from_completed else "completed_interview"
        )
        wrap_text = await _generate_wrap_text(
            purpose=purpose,
            context=(
                f"The interview ended with reason {end_reason}. "
                f"Unfinished agenda items: {len(unfinished)}. "
                "Acknowledge the actual ending without claiming the interview was fully completed."
            ),
            is_vi=ctx.is_vi,
        )
    await db.execute(
        text(
            """
            INSERT INTO interview_chat_messages
            (id, session_id, role, content, metadata, turn_id, message_type, sequence, created_at)
            VALUES
            (:id, :sid, 'assistant', :content, '{}'::jsonb, NULL, 'WRAP_UP', :seq, now())
            """
        ),
        {"id": commit.asst_msg_id, "sid": ctx.session_id, "content": wrap_text, "seq": claim.asst_seq},
    )
    await db.execute(
        text(
            """
            UPDATE interview_sessions
            SET status = 'CLOSED', end_reason = :reason, ended_at = now(), updated_at = now()
            WHERE id = :sid
            """
        ),
        {"sid": ctx.session_id, "reason": end_reason},
    )
    await db.commit()

    provenance = _decision_trace_fields(out, session_state)
    provenance["transition_reason"] = provenance["transition_reason"] or end_reason
    trace_event(
        "interviewer",
        "stage_transition_committed",
        session_id=ctx.session_id,
        turn_id=ctx.turn_id,
        turn_index=ctx.turn_index,
        from_stage=ctx.stage,
        to_stage="CLOSED",
        next_turn_id=None,
        next_turn_index=None,
        end_reason=end_reason,
        **provenance,
    )

    now_iso = datetime.now(UTC).isoformat()
    return _turn_response(
        ctx,
        stage="CLOSING",
        turn_index=ctx.turn_index,
        action="COMPLETE",
        message=wrap_text,
        remaining_time=remaining_time,
        probe_count=commit.prepared.probes_this_turn,
        completed=True,
        user=commit.candidate_message(now_iso),
        assistant=commit.assistant_message(now_iso, message_type="WRAP_UP", content=wrap_text, turn_id=None),
        turn_status=_turn_status(ctx.turn_id, ctx.turn_index, is_follow_up=False, completed=True),
        session_status="CLOSED",
        endReason=end_reason,
    )


# ------------------------------------------------------------ orchestrator


async def process_candidate_message(
    db: AsyncSession,
    session_row: dict[str, Any],
    *,
    client_message_id: str | None,
    content: str,
    telemetry: dict[str, Any] | None = None,
    modality: str = "CHAT",
    duration_seconds: float = 0.0,
    precomputed_evaluation: dict[str, Any] | None = None,
    precomputed_decision: dict[str, Any] | None = None,
    on_early_speech: Any = None,
    elapsed_override_seconds: float | None = None,
    core_engine: InterviewCoreEngine | None = None,
    preview_key: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Process one final candidate turn.

    `duration_seconds` is how long THIS answer took and only drives the
    per-question hard-answer timeout. `elapsed_override_seconds` is how much of
    the whole session has elapsed and is for callers that track it out of band;
    when omitted the session clock comes from `started_at`.

    Phases (see the module comment above the phase helpers):

    1. load the context, claim the message (lock → idempotency → write → commit);
    2. prepare the state and run the authoritative inference, outside any lock;
    3. re-take the lock, apply exactly one domain transition, commit, respond.
    """
    ctx = await _load_turn_context(
        db, session_row, content, elapsed_override_seconds=elapsed_override_seconds
    )
    claim = await _claim_candidate_message(
        db, ctx, client_message_id=client_message_id, telemetry=telemetry, preview_key=preview_key
    )
    if claim.reply is not None:
        return claim.reply
    if preview_key is not None and not claim.preview_valid:
        # The preview's identity moved between redemption and the claim: run
        # the canonical inference instead of executing a decision made for a
        # turn that is no longer in that state.
        precomputed_evaluation = None
        precomputed_decision = None

    # =========================================================================
    # PHASE 2: Controller & Decision Engine (Outside DB lock)
    # =========================================================================
    prepared = await _prepare_inference(
        db,
        ctx,
        claim,
        modality=modality,
        duration_seconds=duration_seconds,
        precomputed_evaluation=precomputed_evaluation,
        precomputed_decision=precomputed_decision,
        core_engine=core_engine,
    )
    asst_msg_id = str(uuid4())
    core_output = await _run_authoritative_inference(
        db, ctx, prepared, modality=modality, on_early_speech=on_early_speech
    )
    await _persist_turn_counters(db, ctx, core_output, prepared.session_state)

    # =========================================================================
    # PHASE 3: Commit Assistant Response & Advance Turn
    # =========================================================================
    # Re-take the session lock released by the Phase 1 commit. If the candidate
    # ended the session while the reply was being generated, do not append a
    # question to a CLOSED session or overwrite its end reason.
    status_after_llm = await db.scalar(
        text("SELECT status FROM interview_sessions WHERE id = :sid FOR UPDATE"),
        {"sid": ctx.session_id},
    )
    if status_after_llm == "CLOSED":
        await db.rollback()
        raise ChatRuntimeError("SESSION_CLOSED: Phiên phỏng vấn đã kết thúc trong lúc xử lý câu trả lời.")
    if await expire_chat_session_if_due(db, session_row, has_started=True):
        raise ChatRuntimeError("SESSION_CLOSED: Thời gian phỏng vấn đã hết trong lúc xử lý câu trả lời.")

    commit = TurnCommit(
        ctx=ctx,
        claim=claim,
        prepared=prepared,
        core_output=core_output,
        asst_msg_id=asst_msg_id,
        remaining_time=prepared.session_state.get("remaining_time", 0),
    )
    if core_output.action == TurnAction.CONFIRM_ABORT:
        return await _commit_abort(db, commit)
    if core_output.action == TurnAction.CLARIFY:
        return await _commit_clarify(db, commit)
    if core_output.action == TurnAction.PROBE:
        return await _commit_probe(db, commit)
    is_in_closing = (ctx.current_turn.get("question_snapshot") or {}).get("stage") == "CLOSING"
    if is_in_closing and not core_output.is_session_finished:
        return await _commit_closing_reply(db, commit)

    # Advance to NEXT_TOPIC
    next_turn = await _settle_current_turn(db, commit)
    if next_turn and not core_output.is_session_finished:
        return await _ask_next_question(db, commit, next_turn)
    # All turns finished or terminated
    return await _close_session(db, commit)


async def complete_chat_session(
    db: AsyncSession,
    session_row: dict[str, Any],
    reason: EndReason = "USER_ENDED",
) -> dict[str, Any]:
    session_id = session_row["id"]
    is_vi = (session_row.get("locale") or "vi").lower().startswith("vi")

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
            "summary": (
                "Phiên phỏng vấn đã kết thúc. Bạn có thể xem lại toàn bộ nội dung trò chuyện."
                if is_vi
                else "The interview session has ended. You can review the full transcript."
            ),
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
            "summary": (
                "Phiên phỏng vấn đã kết thúc. Bạn có thể xem lại toàn bộ nội dung trò chuyện."
                if is_vi
                else "The interview session has ended. You can review the full transcript."
            ),
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
        wrap_text = await _generate_wrap_text(
            purpose="candidate_ended_session" if reason == "USER_ENDED" else "session_timeout",
            context=(
                "The candidate ended the interview early. Acknowledge this without pressure."
                if reason == "USER_ENDED"
                else "The session time expired. State that the session is ending and submitted answers will be evaluated."
            ),
            is_vi=is_vi,
        )
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
    session_row["end_reason"] = reason
    session_row["ended_at"] = datetime.now(UTC)

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
    return {
        "sessionId": session_id,
        "sessionStatus": "CLOSED",
        "endReason": reason,
        "endedAt": ended_str,
        "summary": summary_text,
    }
