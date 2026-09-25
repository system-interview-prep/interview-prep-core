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

MessageType = Literal[
    "GREETING",
    "MAIN_QUESTION",
    "CLARIFY",
    "PROBE",
    "CANDIDATE_ANSWER",
    "ACKNOWLEDGMENT",
    "WRAP_UP",
]

EndReason = Literal["COMPLETED", "USER_ENDED", "TECHNICAL_FAILURE"]

SAFE_FALLBACK_PROBE_VI = (
    "Bạn có thể phân tích rõ hơn về lý do bạn lựa chọn giải pháp này "
    "và điểm hạn chế cần lưu ý của nó không?"
)
SAFE_FALLBACK_PROBE_EN = (
    "Could you elaborate on the main reasoning behind this approach "
    "and any trade-offs you considered?"
)


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
                "competency": (
                    current_turn.get("question_snapshot", {})
                    .get("target", {})
                    .get("conceptId")
                    if isinstance(current_turn.get("question_snapshot"), dict)
                    else None
                )
                or "Chuyên môn",
                "questionVersionId": str(current_turn["question_version_id"])
                if current_turn["question_version_id"]
                else None,
            }
            if current_turn
            else None
        ),
        "messages": messages,
        "isAwaitingCandidate": is_awaiting,
    }


async def start_chat_session(
    db: AsyncSession,
    session_row: dict[str, Any],
) -> dict[str, Any]:
    session_id = session_row["id"]
    if session_row.get("experience_type") != "interview_chat":
        raise ChatRuntimeError("This session is not an interview chat")
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

    # 1. Greeting message
    if is_vi:
        greeting_text = (
            f"Chào bạn, tôi là AI Interviewer của INTERVIA. Hôm nay chúng ta sẽ cùng phỏng vấn "
            f"cho vị trí {job_title} dựa trên yêu cầu công việc và hồ sơ của bạn. "
            f"Hãy trả lời một cách tự nhiên và cụ thể kinh nghiệm thực tế của bạn nhé. "
            f"Chúng ta sẽ bắt đầu ngay với chủ đề đầu tiên."
        )
    else:
        greeting_text = (
            f"Hello, I am the INTERVIA AI Interviewer. Today we will conduct an interview "
            f"for the {job_title} position based on your resume and job requirements. "
            f"Please answer naturally and share specific insights from your experience. "
            f"Let's begin with our first topic."
        )

    greeting_id = str(uuid4())
    await db.execute(
        text(
            """
            INSERT INTO interview_chat_messages
            (id, session_id, role, content, metadata, turn_id, message_type, sequence, created_at)
            VALUES
            (:id, :sid, 'assistant', :content, '{}'::jsonb, NULL, 'GREETING', 1, now())
            """
        ),
        {"id": greeting_id, "sid": session_id, "content": greeting_text},
    )

    # 2. Main Question message
    q_snapshot = first_turn.get("question_snapshot") or {}
    q_text = q_snapshot.get("questionText") or q_snapshot.get("question_text")
    if not q_text or not first_turn.get("question_version_id"):
        raise ChatRuntimeError("Frozen question is incomplete; cannot start interview chat")

    question_id = str(uuid4())
    q_meta = {
        "questionVersionId": str(first_turn["question_version_id"])
        if first_turn.get("question_version_id")
        else None,
        "turnIndex": 0,
    }
    await db.execute(
        text(
            """
            INSERT INTO interview_chat_messages
            (id, session_id, role, content, metadata, turn_id, message_type, sequence, created_at)
            VALUES
            (:id, :sid, 'assistant', :content, CAST(:metadata AS jsonb), :turn_id, 'MAIN_QUESTION', 2, now())
            """
        ),
        {
            "id": question_id,
            "sid": session_id,
            "content": q_text,
            "metadata": json.dumps(q_meta),
            "turn_id": first_turn["id"],
        },
    )
    await db.commit()

    return await get_chat_runtime(db, session_row)


def _validate_probe_text(probe_text: str) -> bool:
    """Post-generation validator for probe questions.

    Rejects probes that leak scoring, grading, rubrics, or exceed length limit.
    """
    cleaned = probe_text.strip()
    if not cleaned or len(cleaned) > 280:
        return False

    # Prohibited leak patterns
    prohibited_patterns = [
        r"\b(điểm|điểm số|thang điểm|barem|rubric|tiêu chí|bạn được|bạn đạt)\b",
        r"\b(score|scores|grade|grading|rubrics|criterion|criteria|points)\b",
        r"\b\d+\s*/\s*10\b",
        r"\b\d+\s*điểm\b",
    ]
    for pattern in prohibited_patterns:
        if re.search(pattern, cleaned, re.IGNORECASE):
            return False

    return True


async def _decide_next_step(
    *,
    is_vi: bool,
    job_title: str,
    question_text: str,
    candidate_answer: str,
    has_probed: bool,
    working_memory: str = "",
) -> tuple[MessageType, str]:
    """Controller decision engine with strict guardrails, prompt isolation, and validation."""
    # Hard limit: if already probed once, must advance
    if has_probed:
        return "NEXT_TOPIC", ""

    cleaned_answer = candidate_answer.strip().lower()

    # Early refusal or skip keywords
    refusal_keywords = [
        "không biết",
        "chưa rõ",
        "không rõ",
        "bỏ qua",
        "qua câu",
        "don't know",
        "no idea",
        "skip",
    ]
    if any(k in cleaned_answer for k in refusal_keywords) and len(cleaned_answer) < 40:
        return "NEXT_TOPIC", ""

    # Extremely short answer -> neutral clarification
    if len(cleaned_answer) < 25:
        if is_vi:
            clarify_text = "Bạn có thể chia sẻ cụ thể hơn hoặc đưa ra ví dụ thực tế liên quan đến câu hỏi này không?"
        else:
            clarify_text = "Could you elaborate more or provide a concrete example related to this question?"
        return "CLARIFY", clarify_text

    # Prompt isolation: ONLY pass current question & current candidate answer
    instructions = (
        "You are an expert technical interviewer conducting an interview for "
        f"the position of '{job_title}'.\n"
        "Your goal: Decide whether to ask ONE follow-up probe question (PROBE) or advance to the next topic (NEXT_TOPIC).\n"
        "Rules:\n"
        "1. Never invent projects or experience not mentioned by the candidate.\n"
        "2. Never leak evaluation rubrics, scoring criteria, or grades/points.\n"
        "3. If candidate's answer introduces a specific technical solution, ask a short, insightful follow-up question (PROBE) about trade-offs, architecture, edge cases, or reasons behind their choice.\n"
        "4. If candidate's answer is already exhaustive or complete, choose NEXT_TOPIC.\n"
        f"5. Output language MUST BE {'Vietnamese' if is_vi else 'English'}.\n"
        "6. Return ONLY a valid JSON object with keys:\n"
        '   - "decision": "PROBE" or "NEXT_TOPIC"\n'
        '   - "reply_text": "your follow-up question if PROBE, or empty string if NEXT_TOPIC"'
    )
    user_input = (
        f"Main Question: {question_text}\n"
        f"Candidate Answer: {candidate_answer}\n"
    )

    try:
        raw_res = await generate_text(
            instructions=instructions,
            input_text=user_input,
            max_output_tokens=300,
            temperature=0.2,
        )
        data = json.loads(raw_res)
        dec = str(data.get("decision", "NEXT_TOPIC")).upper()
        reply = str(data.get("reply_text", "")).strip()

        if dec == "PROBE" and reply:
            # Deterministic post-validator
            if _validate_probe_text(reply):
                return "PROBE", reply
            # If validation fails, use safe deterministic fallback
            safe_probe = SAFE_FALLBACK_PROBE_VI if is_vi else SAFE_FALLBACK_PROBE_EN
            return "PROBE", safe_probe

        return "NEXT_TOPIC", ""
    except Exception:
        # Graceful fallback heuristic
        if len(candidate_answer.split()) < 25:
            safe_probe = SAFE_FALLBACK_PROBE_VI if is_vi else SAFE_FALLBACK_PROBE_EN
            return "PROBE", safe_probe
        return "NEXT_TOPIC", ""


async def process_candidate_message(
    db: AsyncSession,
    session_row: dict[str, Any],
    *,
    client_message_id: str | None,
    content: str,
) -> dict[str, Any]:
    session_id = session_row["id"]
    if session_row["status"] == "CLOSED":
        raise ChatRuntimeError("Phiên phỏng vấn đã kết thúc.")

    if not client_message_id:
        raise ValueError("clientMessageId is required for reliable retries")
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
            if existing_user_msg["content"] != cleaned_content:
                raise ChatRuntimeError("clientMessageId was already used for different content")
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
            if asst_msg is None:
                raise ChatRuntimeError("Previous response is pending; retry after recovery")
            return {
                "userMessage": _message_payload(dict(existing_user_msg)),
                "assistantResponse": _message_payload(dict(asst_msg)),
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

    # Check for early exit request
    lower_c = cleaned_content.lower()
    early_exit_requested = any(
        k in lower_c
        for k in [
            "dừng phỏng vấn",
            "kết thúc phỏng vấn",
            "tôi muốn dừng",
            "stop interview",
            "end interview",
            "quit interview",
        ]
    )

    asst_msg_id = str(uuid4())

    if early_exit_requested:
        wrap_text = (
            "Cảm ơn bạn. Buổi phỏng vấn sẽ kết thúc tại đây theo nguyện vọng của bạn. "
            "Chúc bạn một ngày làm việc hiệu quả và thành công!"
            if is_vi
            else "Thank you. The interview will conclude here per your request. Have a great day!"
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
                SET status = 'CLOSED', end_reason = 'USER_ENDED', ended_at = now(), updated_at = now()
                WHERE id = :sid
                """
            ),
            {"sid": session_id},
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
            "endReason": "USER_ENDED",
        }

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

    q_snap = current_turn.get("question_snapshot") or {}
    q_text = q_snap.get("questionText") or q_snap.get("question_text") or ""

    decision, reply_text = await _decide_next_step(
        is_vi=is_vi,
        job_title=job_title,
        question_text=q_text,
        candidate_answer=cleaned_content,
        has_probed=has_probed,
    )

    # =========================================================================
    # PHASE 3: Commit Assistant Response & Advance Turn
    # =========================================================================
    if decision in {"PROBE", "CLARIFY"}:
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
                "content": reply_text,
                "turn_id": current_turn_id,
                "msg_type": decision,
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
                "messageType": decision,
                "turnId": current_turn_id,
                "sequence": asst_seq,
                "content": reply_text,
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
    if next_turn:
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
        next_q_text = next_snap.get("questionText") or next_snap.get("question_text")
        if not next_q_text or not next_turn.get("question_version_id"):
            raise ChatRuntimeError("Frozen question is incomplete; cannot advance interview chat")

        # Bridge Transition: natural acknowledgment + next main question
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

    # All turns finished -> WRAP_UP with end_reason = 'COMPLETED'
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
            SET status = 'CLOSED', end_reason = 'COMPLETED', ended_at = now(), updated_at = now()
            WHERE id = :sid
            """
        ),
        {"sid": session_id},
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
