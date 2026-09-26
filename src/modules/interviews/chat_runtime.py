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
from typing import Any, Literal
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.ai.facade import generate_text
from src.modules.interviews.core.interview_engine import (
    SAFE_FALLBACK_PROBE_EN,
    SAFE_FALLBACK_PROBE_VI,
    InterviewCoreEngine,
    validate_probe_text,
)
from src.modules.interviews.core.interview_types import (
    CandidateTurnInput,
    InterviewerTurnOutput,
    InterviewStage,
    SessionExitReason,
    TurnAction,
)

_validate_probe_text = validate_probe_text

MessageType = Literal[
    "GREETING",
    "MAIN_QUESTION",
    "CLARIFY",
    "PROBE",
    "CANDIDATE_ANSWER",
    "ACKNOWLEDGMENT",
    "WRAP_UP",
    "CONFIRM_ABORT",
]

EndReason = Literal["COMPLETED", "USER_ENDED", "TECHNICAL_FAILURE"]


class ChatRuntimeError(RuntimeError):
    pass


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
            SELECT id, turn_index, status, question_version_id, question_snapshot
            FROM interview_turns
            WHERE session_id = :session_id
            ORDER BY turn_index ASC
            """
        ),
        {"session_id": session_id},
    )
    turns = [dict(r) for r in turns_res.mappings().all()]

    current_turn = next(
        (t for t in turns if t["status"] in {"PLANNED", "ASKED"}),
        None,
    )
    current_turn_index = current_turn["turn_index"] if current_turn else len(turns)

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
                "questionVersionId": str(current_turn["question_version_id"])
                if current_turn["question_version_id"]
                else None,
                "questionType": (current_turn.get("question_snapshot") or {}).get("questionType") or "technical",
                "starterCode": (current_turn.get("question_snapshot") or {}).get("starterCode"),
                "testCasesCode": (current_turn.get("question_snapshot") or {}).get("testCasesCode"),
                "language": (current_turn.get("question_snapshot") or {}).get("language", "python"),
            }
            if current_turn
            else None
        ),
        "messages": messages,
        "isAwaitingCandidate": is_awaiting,
        "durationMinutes": int(session_row.get("duration_minutes") or 25),
        "startedAt": session_row.get("started_at").isoformat() if session_row.get("started_at") else None,
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
    if session_row["status"] == "CLOSED":
        raise ChatRuntimeError("Phiên phỏng vấn đã kết thúc.")
    if session_row.get("plan_status") != "LOCKED":
        raise ChatRuntimeError(
            "Phiên phỏng vấn phải được chuẩn bị câu hỏi (LOCKED) trước khi bắt đầu chat."
        )

    # Check if already started in interview_chat_messages
    existing_count = await db.scalar(
        text("SELECT COUNT(*) FROM interview_chat_messages WHERE session_id = :sid"),
        {"sid": session_id},
    )
    if existing_count and existing_count > 0:
        return await get_chat_runtime(db, session_row)

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
        raise ChatRuntimeError("Không tìm thấy bộ câu hỏi nào cho phiên này.")

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

    # Single opening message: greeting + first turn question
    q_snapshot = first_turn.get("question_snapshot") or {}
    q_text = (
        q_snapshot.get("questionText")
        or q_snapshot.get("question_text")
        or (
            f"Để bắt đầu và giúp bạn thoải mái hơn, bạn hãy giới thiệu đôi nét về bản thân và kinh nghiệm làm việc gần đây của mình nhé?"
            if is_vi
            else "To help you get comfortable, please give a brief introduction of yourself and your recent experience."
        )
    )

    if "Chào bạn" not in q_text and "Hello" not in q_text:
        opening_text = (
            f"Chào bạn, tôi là AI Interviewer của INTERVIA. Hôm nay chúng ta sẽ cùng phỏng vấn "
            f"cho vị trí {job_title} dựa trên yêu cầu công việc và hồ sơ của bạn.\n\n"
            f"{q_text}"
            if is_vi
            else f"Hello, I am the INTERVIA AI Interviewer. Today we will conduct an interview "
            f"for the {job_title} position based on your resume and job requirements.\n\n"
            f"{q_text}"
        )
    else:
        opening_text = q_text

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
) -> dict[str, Any]:
    """Trích xuất và ánh xạ dữ liệu session/turns sang session_state cho InterviewCoreEngine."""
    session_id = session_row["id"]
    snap = (current_turn.get("question_snapshot") or {}) if current_turn else {}
    current_stage = (snap.get("stage") if current_turn else None) or InterviewStage.DEEP_DIVE.value
    q_text = snap.get("questionText") or snap.get("question_text") or ""
    rubric_criteria = _format_rubric_criteria((snap.get("rubric") or {}).get("criteria", ""))

    asked_question_ids = [
        str(t.get("question_version_id") or t["id"])
        for t in turns
        if t.get("status") in {"ANSWERED", "COMPLETED"}
    ]
    if current_turn and has_probed:
        current_qid = str(current_turn.get("question_version_id") or current_turn["id"])
        if current_qid not in asked_question_ids:
            asked_question_ids.append(current_qid)

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
            "rubric_criteria": _format_rubric_criteria((t_snap.get("rubric") or {}).get("criteria", "")),
        })

    metadata_json = session_row.get("metadata") or {}
    if isinstance(metadata_json, str):
        try:
            metadata_json = json.loads(metadata_json)
        except Exception:
            metadata_json = {}

    return {
        "session_id": session_id,
        "current_stage": current_stage,
        "consecutive_fails": metadata_json.get("consecutive_fails", 0),
        "current_turn_in_question": 1 if has_probed else 0,
        "target_duration_minutes": session_row.get("duration_minutes") or 25,
        "started_at": session_row.get("started_at"),
        "locale": session_row.get("locale") or "vi-VN",
        "allow_early_exit": metadata_json.get("allow_early_exit", True),
        "asked_question_ids": asked_question_ids,
        "current_question_context": {
            "question_id": str(current_turn.get("question_version_id") or current_turn["id"]) if current_turn else "",
            "main_prompt": q_text,
            "rubric_criteria": rubric_criteria,
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
    core_engine: InterviewCoreEngine | None = None,
) -> dict[str, Any]:
    session_id = session_row["id"]
    if session_row["status"] == "CLOSED":
        raise ChatRuntimeError("Phiên phỏng vấn đã kết thúc.")

    cleaned_content = content.strip()
    if not cleaned_content:
        raise ValueError("Nội dung tin nhắn không được để trống.")

    # Idempotency check: if client_message_id already exists, return existing reply immediately
    if client_message_id:
        existing = await db.execute(
            text(
                """
                SELECT id, session_id, role, content, metadata, created_at,
                       turn_id, message_type, sequence, client_message_id
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
            return {
                "userMessage": _message_payload(dict(existing_user_msg)),
                "assistantResponse": _message_payload(dict(asst_msg)) if asst_msg else None,
                "turnStatus": {"completed": False},
                "sessionStatus": session_row["status"],
            }

    # Fetch turns
    turns_res = await db.execute(
        text(
            """
            SELECT id, turn_index, status, question_version_id, question_snapshot, answer_text
            FROM interview_turns
            WHERE session_id = :session_id
            ORDER BY turn_index ASC
            """
        ),
        {"session_id": session_id},
    )
    turns = [dict(r) for r in turns_res.mappings().all()]
    if not turns:
        raise ChatRuntimeError("Không tìm thấy lượt câu hỏi nào.")

    current_turn = next((t for t in turns if t["status"] in {"ASKED", "PLANNED"}), None)
    if current_turn is None:
        raise ChatRuntimeError("Tất cả các câu hỏi trong phiên đã hoàn thành.")

    current_turn_id = current_turn["id"]
    current_turn_index = current_turn["turn_index"]

    # =========================================================================
    # PHASE 1: Fast Write Candidate Message (Committed immediately)
    # =========================================================================
    max_seq = await db.scalar(
        text("SELECT COALESCE(MAX(sequence), 0) FROM interview_chat_messages WHERE session_id = :sid"),
        {"sid": session_id},
    )
    user_seq = int(max_seq or 0) + 1
    asst_seq = user_seq + 1

    user_msg_id = str(uuid4())
    await db.execute(
        text(
            """
            INSERT INTO interview_chat_messages
            (id, session_id, role, content, metadata, turn_id, message_type, sequence, client_message_id, created_at)
            VALUES
            (:id, :sid, 'user', :content, '{}'::jsonb, :turn_id, 'CANDIDATE_ANSWER', :seq, :cid, now())
            """
        ),
        {
            "id": user_msg_id,
            "sid": session_id,
            "content": cleaned_content,
            "turn_id": current_turn_id,
            "seq": user_seq,
            "cid": client_message_id,
        },
    )

    # Accumulate answer text into interview_turns
    existing_answer = current_turn.get("answer_text") or ""
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
    # Commit Phase 1: User message is securely stored
    await db.commit()

    # =========================================================================
    # PHASE 2: Controller & Decision Engine (Outside DB lock)
    # =========================================================================
    is_vi = (session_row.get("locale") or "vi").lower().startswith("vi")
    job_title = await _get_job_title(db, session_row.get("job_id"))

    asst_msg_id = str(uuid4())

    # Count how many probes already occurred in this turn
    probes_count = await db.scalar(
        text(
            """
            SELECT COUNT(*) FROM interview_chat_messages
            WHERE session_id = :sid AND turn_id = :turn_id
              AND role = 'assistant' AND message_type IN ('PROBE', 'CLARIFY')
            """
        ),
        {"sid": session_id, "turn_id": current_turn_id},
    )
    has_probed = bool(probes_count and probes_count >= 1)

    # Call Modality-Agnostic Core Engine
    engine = core_engine or InterviewCoreEngine(ai_generator=generate_text)
    session_state = build_session_state_from_db(session_row, turns, current_turn, has_probed)

    turn_input = CandidateTurnInput(
        session_id=session_id,
        turn_index=current_turn_index,
        text_content=cleaned_content,
        modality="CHAT",
        duration_seconds=0.0,
        telemetry=telemetry or {},
    )

    core_output = await engine.handle_turn(turn_input, session_state)

    # Persist updated consecutive_fails and allow_early_exit in session metadata
    meta = dict(session_row.get("metadata") or {})
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except Exception:
            meta = {}
    meta["consecutive_fails"] = core_output.metadata.get("consecutive_fails", 0)
    meta["allow_early_exit"] = session_state.get("allow_early_exit", True)
    session_row["metadata"] = meta
    await db.execute(
        text("UPDATE interview_sessions SET metadata = CAST(:meta AS jsonb), updated_at = now() WHERE id = :sid"),
        {"meta": json.dumps(meta), "sid": session_id},
    )

    # =========================================================================
    # PHASE 3: Commit Assistant Response & Advance Turn
    # =========================================================================
    # KÍCH HOẠT DUAL-TRIGGER CONFIRM_ABORT MODAL
    if core_output.action == TurnAction.CONFIRM_ABORT:
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
            "action": "CONFIRM_ABORT",
            "turnStatus": {
                "turnId": current_turn_id,
                "turnIndex": current_turn_index,
                "isFollowUp": False,
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
                (:id, :sid, 'assistant', :content, '{}'::jsonb, :turn_id, :msg_type, :seq, now())
                """
            ),
            {
                "id": asst_msg_id,
                "sid": session_id,
                "content": core_output.message_text,
                "turn_id": current_turn_id,
                "msg_type": "PROBE",
                "seq": asst_seq,
            },
        )
        await db.commit()
        return {
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

    next_turn = next((t for t in turns if t["turn_index"] == current_turn_index + 1), None)
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
        next_q_text = (
            next_snap.get("questionText")
            or next_snap.get("question_text")
            or "Câu hỏi tiếp theo dành cho bạn."
        )

        # Bridge Transition: natural acknowledgment + next main question
        next_content = core_output.message_text
        if not next_content or next_q_text not in next_content:
            if is_vi:
                next_content = (
                    f"Cảm ơn chia sẻ thực tế của bạn. Chúng ta hãy cùng tiếp nối sang chủ đề tiếp theo:\n\n"
                    f"{next_q_text}"
                )
            else:
                next_content = (
                    f"Thank you for your insights. Let's move on to our next topic:\n\n"
                    f"{next_q_text}"
                )

        q_meta = {
            "questionVersionId": str(next_turn["question_version_id"])
            if next_turn.get("question_version_id")
            else None,
            "turnIndex": next_turn["turn_index"],
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
                "id": asst_msg_id,
                "sid": session_id,
                "content": next_content,
                "metadata": json.dumps(q_meta),
                "turn_id": next_turn["id"],
                "seq": asst_seq,
            },
        )
        await db.commit()
        return {
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
        else:
            end_reason = "COMPLETED"
    elif not next_turn:
        end_reason = "COMPLETED"

    wrap_text = core_output.message_text if core_output.is_session_finished else ""
    if not wrap_text:
        if is_vi:
            wrap_text = (
                "Cảm ơn bạn đã tham gia buổi phỏng vấn hôm nay! Chúng ta đã hoàn thành tất cả các chủ đề "
                "chuyên môn theo kế hoạch. Bạn có thể xem lại toàn bộ nội dung trò chuyện tại đây. "
                "Chúc bạn luôn gặt hái nhiều thành công!"
            )
        else:
            wrap_text = (
                "Thank you for participating in today's interview! We have completed all the planned "
                "topics. You can review the full conversation transcript here. "
                "Wishing you great success!"
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
        "endReason": "COMPLETED",
    }


async def complete_chat_session(
    db: AsyncSession,
    session_row: dict[str, Any],
    reason: EndReason = "USER_ENDED",
) -> dict[str, Any]:
    session_id = session_row["id"]
    is_vi = (session_row.get("locale") or "vi").lower().startswith("vi")

    if session_row["status"] != "CLOSED":
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
            wrap_text = (
                "Phiên phỏng vấn đã kết thúc theo yêu cầu của bạn. "
                "Cảm ơn bạn đã dành thời gian tham gia!"
                if is_vi
                else "The interview session has been concluded per your request. Thank you for your time!"
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
