# src/modules/interviews/core/interview_engine.py
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple
from uuid import UUID

from src.core.trace_logging import trace_event
from src.modules.ai.facade import generate_text
from src.modules.interviews.core.demo_mode import DEMO_OVERTIME_GRACE_SECONDS
from src.modules.interviews.core.interview_types import (
    CandidateTurnInput,
    InterviewerTurnOutput,
    InterviewStage,
    QuestionItem,
    SessionExitReason,
    TurnAction,
)

logger = logging.getLogger("InterviewCoreEngine")

TIME_THRESHOLDS = {
    InterviewStage.WARM_UP: 0.10,     # Chiếm 10% đầu
    InterviewStage.VALIDATE: 0.30,    # Chiếm đến 30% tổng thời gian
    InterviewStage.DEEP_DIVE: 0.75,   # Kéo dài từ 30% đến 75%
    InterviewStage.CHALLENGE: 0.85,   # Kéo dài đến 85%
    InterviewStage.BEHAVIORAL: 0.95,  # Kéo dài đến 95%
    InterviewStage.CLOSING: 1.00,     # 5% cuối cùng
}

PROHIBITED_LEAK_PATTERNS = [
    r"\b(điểm|điểm số|thang điểm|barem|rubric|tiêu chí|bạn được|bạn đạt)\b",
    r"\b(score|scores|grade|grading|rubrics|criterion|criteria|points)\b",
    r"\b\d+\s*/\s*10\b",
    r"\b\d+\s*điểm\b",
]

SAFE_FALLBACK_PROBE_VI = (
    "Bạn có thể phân tích rõ hơn về lý do bạn lựa chọn giải pháp này "
    "và điểm hạn chế cần lưu ý của nó không?"
)
SAFE_FALLBACK_PROBE_EN = (
    "Could you elaborate on the main reasoning behind this approach "
    "and any trade-offs you considered?"
)


def validate_probe_text(probe_text: str) -> bool:
    """Kiểm tra câu hỏi probe có hợp lệ và không rò rỉ barem/rubric hay không."""
    cleaned = probe_text.strip()
    if not cleaned or len(cleaned) > 280:
        return False
    for pattern in PROHIBITED_LEAK_PATTERNS:
        if re.search(pattern, cleaned, re.IGNORECASE):
            return False
    return True

ACKNOWLEDGEMENT_MAX_CHARS = 120

# Closing Q&A budget: at most this many candidate questions, and at most
# QNA_BUDGET_SECONDS (scaled like the other pacing thresholds) in total.
MAX_QNA_QUESTIONS = 2
QNA_BUDGET_SECONDS = 180
_JOB_TEXT_LIMIT = 1500


def format_job_context(job: dict[str, Any] | None, is_vi: bool) -> str:
    """Render the posting facts the closing Q&A may rely on, and nothing else."""
    job = job or {}
    lines: list[str] = []

    def add(label_vi: str, label_en: str, value: Any) -> None:
        if value not in (None, "", [], {}):
            lines.append(f"- {label_vi if is_vi else label_en}: {value}")

    add("Công ty", "Company", job.get("company_name"))
    add("Vị trí", "Position", job.get("title"))
    add("Địa điểm", "Location", job.get("location"))
    add("Hình thức làm việc", "Work mode", job.get("work_mode"))
    add("Loại hợp đồng", "Employment type", job.get("employment_type"))
    add("Cấp bậc", "Seniority", job.get("seniority"))
    if job.get("salary_min") or job.get("salary_max"):
        low, high = job.get("salary_min") or "?", job.get("salary_max") or "?"
        salary = f"{low} - {high} {job.get('salary_currency') or ''}".strip()
        add("Mức lương", "Salary", salary)
    elif job.get("salary_negotiable"):
        add("Mức lương", "Salary", "thoả thuận" if is_vi else "negotiable")
    add("Mô tả công việc", "Job description", str(job.get("description") or "")[:_JOB_TEXT_LIMIT])
    add("Yêu cầu", "Requirements", str(job.get("requirements") or "")[:_JOB_TEXT_LIMIT])
    return "\n".join(lines) or ("(Không có thông tin tuyển dụng)" if is_vi else "(No posting information)")


# Pacing thresholds below are tuned for a 25-minute session.
_PACING_REFERENCE_SECONDS = 25 * 60


def pacing_seconds(default_seconds: int, target_duration_seconds: int) -> int:
    """Scale a pacing threshold down for sessions shorter than 25 minutes.

    The fixed values (30s hard timeout, 90s cutoff, 60s closing + 180s
    behavioral reserve) consume a short session whole: a 3-minute demo jumped
    to BEHAVIORAL after ~1 minute and never asked a technical question. Sessions
    of 25 minutes or more keep the original values unchanged.
    """
    scaled = round(default_seconds * target_duration_seconds / _PACING_REFERENCE_SECONDS)
    return max(1, min(default_seconds, scaled))


def safe_acknowledgement(raw: Any, is_vi: bool) -> str:
    """Return the evaluator's acknowledgement only if it is a short, neutral line.

    The acknowledgement is LLM output shaped by the candidate's own answer and
    is shown/spoken verbatim, so an injected answer could make the interviewer
    announce a score or say anything else. Anything long, multi-line, asking a
    question or matching the rubric-leak patterns falls back to a fixed line.
    """
    default = "Cảm ơn chia sẻ của bạn." if is_vi else "Thank you for sharing."
    text_value = str(raw or "").strip()
    if (
        not text_value
        or len(text_value) > ACKNOWLEDGEMENT_MAX_CHARS
        or "\n" in text_value
        or "?" in text_value
        or any(re.search(pattern, text_value, re.IGNORECASE) for pattern in PROHIBITED_LEAK_PATTERNS)
    ):
        return default
    return text_value


# Tập từ khóa CHẮC CHẮN LÀ BỎ CUỘC (Không bao giờ nhầm với Yes/No)
DEFINITE_GIVE_UP_KEYWORDS = {
    "ko biết", "không biết", "chịu", "em chịu", "mình chịu", "bỏ qua", "pass",
    "chưa học", "chưa rõ", "không rõ", "qua câu", "don't know", "no idea",
    "skip", "idk", "dunno", "k biet", "k biết", "chua hoc", "chua ro", "bó tay",
    "dont know", "have no idea", "skip question", "chịu thua", "bó tay rồi",
    "em ko biết", "em không biết", "mình không biết"
}

# Tập từ khóa trung tính / mập mờ (Chỉ nghi vấn nếu câu hỏi KHÔNG PHẢI Yes/No)
AMBIGUOUS_KEYWORDS = {
    "ừm", "um", "uhm", "uh", "ơ", "ờ", "k", "ko", "không", "chưa", "ok", "pk",
    "khong", "chua", "chưa ạ", "không ạ", "chưa từng", "dạ chưa", "dạ không"
}


def is_yes_no_question(question_prompt: str) -> bool:
    """Nhận diện câu hỏi dạng Yes/No hoặc câu hỏi khảo sát kinh nghiệm có/chưa."""
    if not question_prompt:
        return False
    q = question_prompt.lower().strip()
    yes_no_patterns = [
        r"^(bạn|em|mình)?\s*(đã|có|từng|có phải|đã từng)",
        r"(chưa|không|phải không|đúng không)\s*\??$",
        r"\b(have you|did you|is it|can you|are you|do you|were you)\b",
        r"\b(từng|đã từng|có bao giờ)\b",
    ]
    return any(re.search(p, q) for p in yes_no_patterns)


# Abort phrases that name the interview itself: unambiguous at any length.
EXPLICIT_ABORT_PATTERNS = [
    r"(dừng|ngừng|kết thúc|hủy|thôi|rút khỏi)\s*(lại\s*)?(buổi\s*|cuộc\s*)?phỏng vấn",
    r"không\s*(muốn\s*)?(phỏng vấn|tiếp tục phỏng vấn|thi)\s*nữa",
    r"\b(stop|end|abort|quit|cancel|leave|finish)\s+(the\s+|this\s+|our\s+)?interview\b",
    r"\b(cannot|can't|can not)\s+continue\s+(the\s+|this\s+)?interview\b",
    r"\bfamily emergency\b",
]

# Generic "stop" phrasing. Technical answers say the same words ("service phải
# dừng lại", "we need to stop the consumer"), so these only count for a short
# message that is plausibly a request on its own.
SOFT_ABORT_PATTERNS = [
    # Xin dừng / xin nghỉ / em muốn dừng ... (first-person request)
    r"(xin|cho\s*(em|mình|tôi)|cho phép|(em|mình|tôi)\s*(muốn|xin|phải|cần|đành))\s*(phép\s*)?(dừng|nghỉ|out|thôi|rút|kết thúc)\b",
    # Có việc bận / việc gia đình / việc riêng đi kèm dừng/nghỉ
    r"(việc\s*(bận|gấp|đột xuất|gia đình|riêng)|bận\s*(rồi|quá|việc|gia đình))\b.*?\b(dừng|nghỉ|thôi|out|về|kết thúc)",
    r"\b(dừng|nghỉ|thôi|out)\b.*?\b(việc\s*(bận|gấp|gia đình|riêng)|bận)",
    # Dừng lại đây / không làm nữa
    r"(dừng\s*(lại\s*)?(đây|ở đây|tại đây|nhé|nha|ạ|thôi)|không\s*(tiếp tục|làm)\s*nữa)",
    r"\b(i|we)\s+(want to|have to|need to|must)\s+(stop|quit|leave|exit|abort)\s*(now|here|early|today)?\s*[.!]*$",
    r"\b(cannot continue|can't continue|stop here)\b",
]
SOFT_ABORT_MAX_CHARS = 100

SKIP_QUESTION_PATTERNS = [
    r"(cho\s*(em|mình)\s*)?(xin\s*)?(đổi|qua|bỏ qua|chuyển|skip)\s*(sang\s+)?(câu|câu hỏi|chủ đề|phần)",
    r"(câu\s*này|phần\s*này)\s*(em|mình)?\s*(chưa|không)\s*(rõ|biết|làm)\b.*?\b(câu khác|chủ đề khác|qua)",
    r"\b(next question|skip question|pass this|skip this question)\b",
]


CLARIFY_PATTERNS = [
    r"(giải thích|nhắc|nói lại|hỏi lại|giải thích lại|làm rõ)\s*(thêm|hộ|cho|rõ hơn|rõ|lại)?\s*(câu hỏi|ý|đề bài|chủ đề)",
    r"(ý|nghĩa)\s*(của\s*)?(bạn|anh|chị|câu hỏi)\s*(là|như thế nào|nghĩa là gì|là sao)",
    r"\b(chưa hiểu|không hiểu|chưa rõ)\s*(rõ\s*)?(câu hỏi|ý|đề|yêu cầu)\b",
    r"\b(could you|can you|please)\s*(clarify|explain|repeat)\b",
    r"\b(what do you mean|pardon|meaning of the question)\b",
    r"^(ý là sao|là sao ạ|nghĩa là sao|chưa hiểu ạ|chưa rõ ạ)\??$",
]


def is_clarify_request(text: str) -> bool:
    """Nhận diện mọi biến thể yêu cầu giải thích / làm rõ câu hỏi của ứng viên."""
    cleaned = text.strip().lower()
    return any(re.search(pattern, cleaned, re.IGNORECASE) for pattern in CLARIFY_PATTERNS)


def is_abort_request(text: str) -> bool:
    """Nhận diện ứng viên xin dừng phỏng vấn.

    Cụm có nhắc tới buổi phỏng vấn luôn được tính; cụm chung chung chỉ được
    tính khi tin nhắn ngắn, để câu trả lời kỹ thuật như "service phải dừng lại
    và rollback" không bị hiểu nhầm là xin dừng.
    """
    cleaned = text.strip().lower()
    if any(re.search(pattern, cleaned, re.IGNORECASE) for pattern in EXPLICIT_ABORT_PATTERNS):
        return True
    return len(cleaned) <= SOFT_ABORT_MAX_CHARS and any(
        re.search(pattern, cleaned, re.IGNORECASE) for pattern in SOFT_ABORT_PATTERNS
    )


def is_skip_request(text: str) -> bool:
    """Nhận diện mọi biến thể xin đổi hoặc bỏ qua câu hỏi của ứng viên."""
    cleaned = text.strip().lower()
    return any(re.search(pattern, cleaned, re.IGNORECASE) for pattern in SKIP_QUESTION_PATTERNS)


def classify_candidate_intent(candidate_text: str, question_prompt: str = "") -> str:
    """
    Phân loại ý định của ứng viên (Context-Aware Intent Classification).
    Trả về:
    - 'CANDIDATE_ABORT': Ứng viên xin dừng phỏng vấn vì lý do cá nhân (Case 7)
    - 'SKIP_REQUEST': Ứng viên xin đổi câu hỏi (Case 3)
    - 'CLARIFY_REQUEST': Ứng viên nhờ làm rõ câu hỏi (Clarification)
    - 'GIVE_UP': Bỏ cuộc, không biết, cộc lốc không hợp tác
    - 'VALID_ANSWER': Câu trả lời hợp lệ (kể cả Yes/No như 'Không', 'Chưa')
    """
    cleaned = candidate_text.strip().lower()

    # 1. Phát hiện xin dừng phỏng vấn sớm (Case 7: Voluntary Abort)
    if is_abort_request(cleaned):
        return "CANDIDATE_ABORT"

    # 2. Phát hiện xin đổi / bỏ qua câu hỏi (Case 3: Skip Request)
    if is_skip_request(cleaned) and len(cleaned) <= 150:
        return "SKIP_REQUEST"

    # 2.5. Phát hiện yêu cầu làm rõ câu hỏi (Clarification Request)
    if is_clarify_request(cleaned) and len(cleaned) <= 150:
        return "CLARIFY_REQUEST"

    # 3. Lớp 1: Chắc chắn là bỏ cuộc (không bao giờ nhầm với Yes/No)
    if cleaned in DEFINITE_GIVE_UP_KEYWORDS:
        return "GIVE_UP"

    give_up_phrases = [
        "không biết", "ko biết", "k biết", "k biet",
        "em chịu", "mình chịu", "bỏ qua", "pass câu", "chưa học",
        "don't know", "dont know", "no idea", "have no idea", "skip question",
        "bó tay", "chưa có kinh nghiệm", "chua co kinh nghiem"
    ]
    if any(p in cleaned for p in give_up_phrases) and len(cleaned) <= 40 and not is_yes_no_question(question_prompt):
        return "GIVE_UP"

    # 4. Lớp 2: Kiểm tra từ mập mờ ("không", "chưa", "ok", "ừm", ...)
    if cleaned in AMBIGUOUS_KEYWORDS:
        # Nếu câu hỏi là Yes/No ("Bạn từng dùng Kafka chưa?" -> "Chưa" / "Không")
        if is_yes_no_question(question_prompt):
            return "VALID_ANSWER"  # Hợp lệ! Không được tính là trượt/bỏ cuộc.
        else:
            return "GIVE_UP"  # Câu hỏi lý thuyết mà nói "Không" / "Ừm" -> Bỏ cuộc.

    # 5. Lớp 3: Câu trả lời quá ngắn (<= 3 ký tự)
    if len(cleaned) <= 3 and not is_yes_no_question(question_prompt):
        return "GIVE_UP"

    return "VALID_ANSWER"


def is_unresponsive_or_give_up(text: str, question_prompt: str = "") -> bool:
    """Nhận diện nhanh các câu trả lời bỏ cuộc, cộc lốc hoặc thiếu hợp tác (có nhận thức ngữ cảnh Yes/No)."""
    intent = classify_candidate_intent(text, question_prompt)
    return intent in ("GIVE_UP", "SKIP_REQUEST")


class InterviewCoreEngine:
    """
    Headless Interview Engine dùng chung 100% cho cả ChatAdapter và VoiceAdapter.
    """

    def __init__(self, llm_client=None, ai_generator=None):
        """
        llm_client: Client gọi LLM hoặc wrapper hỗ trợ generate_json / generate_text.
        ai_generator: Hàm generate async có chữ ký (instructions, input_text, ...).
        Nếu None, mặc định sử dụng module AI facade của dự án.
        """
        self.llm = llm_client
        self._generate_fn = ai_generator or generate_text

    async def handle_turn(
        self,
        turn_input: CandidateTurnInput,
        session_state: Dict[str, Any],
    ) -> InterviewerTurnOutput:
        """
        Hàm xử lý lượt phỏng vấn trung tâm.
        - turn_input: dữ liệu ứng viên vừa gửi (text + metadata)
        - session_state: state lấy từ DB (PostgreSQL session + turns)
        """
        session_id = turn_input.session_id
        candidate_text = turn_input.text_content.strip()
        locale = session_state.get("locale", "vi-VN")
        is_vi = str(locale).lower().startswith("vi")

        current_stage = InterviewStage(session_state.get("current_stage", InterviewStage.WARM_UP))

        # =========================================================================
        # CHỐT CHẶN TOÀN CỤC 1: XIN DỪNG PHỎNG VẤN (CONFIRM_ABORT MODAL)
        # Bất kể đang ở Warm-up, Validate hay bất kỳ stage nào:
        # Nếu ứng viên có dấu hiệu xin dừng -> Kích hoạt Modal xác nhận cho ứng viên tự quyết
        # =========================================================================
        # In CLOSING the candidate wrapping up ("em hết câu hỏi rồi, mình kết
        # thúc tại đây nhé") is the normal ending, not an abort: let the closing
        # handler complete the session instead of recording USER_ENDED.
        if current_stage != InterviewStage.CLOSING and is_abort_request(candidate_text):
            confirm_msg = (
                "Mình đã mở hộp thoại xác nhận kết thúc buổi phỏng vấn ngay bên dưới khung chat. Bạn có thể bấm xác nhận để hoàn tất phiên nhé."
                if is_vi
                else "I have opened the confirmation dialog to end the interview right below in this chat. Please confirm if you wish to conclude."
            )
            return InterviewerTurnOutput(
                session_id=session_id,
                turn_index=turn_input.turn_index + 1,
                message_text=confirm_msg,
                action=TurnAction.CONFIRM_ABORT,
                current_stage=current_stage,
                is_session_finished=False,
                metadata={
                    "requires_abort_confirmation": True,
                    "candidate_abort_intent": True,
                    "consecutive_uncooperative": 0,
                    "consecutive_fails": 0,  # Deprecated compatibility alias.
                },
            )

        now = datetime.now(timezone.utc)

        # 1. TÍNH TOÁN THỜI LƯỢNG & PACING
        # Session elapsed time comes from `session_state`, never from
        # `turn_input.duration_seconds` -- that field is the length of THIS
        # answer and is used below only for the per-question hard-answer
        # timeout. Treating it as session elapsed restarted the session clock on
        # every turn for any caller that supplied it.
        started_at = session_state.get("started_at") or now
        if isinstance(started_at, str):
            try:
                started_at = datetime.fromisoformat(started_at)
            except Exception:
                started_at = now
        if started_at is None:
            started_at = now
        if session_state.get("elapsed_time") is not None and session_state.get("elapsed_time") > 0:
            elapsed_seconds = int(session_state["elapsed_time"])
        else:
            elapsed_seconds = max(0, int((now - started_at).total_seconds()))
        target_duration_seconds = session_state.get("target_duration_minutes", 25) * 60
        time_remaining_seconds = max(0, target_duration_seconds - elapsed_seconds)
        session_state["elapsed_time"] = elapsed_seconds
        session_state["remaining_time"] = time_remaining_seconds

        # Kiểm tra Hard Timeout toàn phiên (<= 30 giây và chưa ở stage CLOSING)
        current_stage = InterviewStage(session_state.get("current_stage", InterviewStage.WARM_UP))
        # A demo follows its turns, not the clock: it only times out once it is
        # past its nominal duration by the overtime grace.
        if session_state.get("is_demo"):
            is_hard_timeout = elapsed_seconds > target_duration_seconds + DEMO_OVERTIME_GRACE_SECONDS
        else:
            is_hard_timeout = (
                time_remaining_seconds <= pacing_seconds(30, target_duration_seconds)
                and current_stage != InterviewStage.CLOSING
            )
        if is_hard_timeout:
            trace_event(
                "interviewer",
                "hard_timeout_triggered",
                session_id=session_id,
                time_remaining_seconds=time_remaining_seconds,
                elapsed_seconds=elapsed_seconds,
            )
            timeout_msg = (
                "Thời lượng buổi phỏng vấn đã hết. Cảm ơn bạn rất nhiều vì đã tham gia. "
                "Toàn bộ câu trả lời đã được lưu lại để hội đồng đánh giá."
                if is_vi
                else "The interview session duration has ended. Thank you for your participation. "
                     "All your answers have been recorded for evaluation."
            )
            return self._terminate_session(
                session_id=session_id,
                turn_index=turn_input.turn_index + 1,
                reason=SessionExitReason.HARD_TIMEOUT,
                message=timeout_msg,
            )

        # =========================================================================
        # CHỐT CHẶN TOÀN CỤC 2: XIN ĐỔI CÂU HỎI (SKIP_REQUEST)
        # =========================================================================
        if is_skip_request(candidate_text):
            skip_ack = (
                "Được chứ, không sao cả. Chúng ta sẽ chuyển sang một chủ đề khác nhé."
                if is_vi
                else "No problem at all. Let's move on to another topic."
            )
            next_stage, next_question = self._get_next_stage_and_question(session_state)
            if next_stage == InterviewStage.CLOSED or next_question is None:
                close_msg = (
                    "Buổi phỏng vấn đã hoàn thành các nội dung. Cảm ơn bạn rất nhiều! "
                    "Hệ thống đang tiến hành tổng hợp báo cáo đánh giá."
                    if is_vi
                    else "The interview has completed all sections. Thank you very much! "
                         "The system is summarizing the evaluation report."
                )
                return self._terminate_session(
                    session_id=session_id,
                    turn_index=turn_input.turn_index + 1,
                    reason=SessionExitReason.NORMAL_COMPLETION,
                    message=close_msg,
                )
            speech = await self._synthesize_interviewer_speech(
                acknowledgement=skip_ack,
                next_question_prompt=next_question.main_prompt,
                is_stage_transition=(next_stage != current_stage),
                is_vi=is_vi,
            )
            return InterviewerTurnOutput(
                session_id=session_id,
                turn_index=turn_input.turn_index + 1,
                message_text=speech,
                action=TurnAction.NEXT_QUESTION,
                current_stage=next_stage,
                current_competency=next_question.competency,
                time_remaining_seconds=time_remaining_seconds,
                metadata={
                    "skipped_turn": True,
                    "turn_in_question": 0,
                    "sufficiency_status": "INSUFFICIENT",
                    "consecutive_uncooperative": 0,
                    "consecutive_fails": 0,  # Deprecated compatibility alias.
                },
            )

        consecutive_uncooperative = session_state.get(
            "consecutive_uncooperative",
            session_state.get("consecutive_fails", 0),
        )
        current_turn_in_question = session_state.get("current_turn_in_question", 0)  # 0: Câu chính, 1: Câu probe
        allow_early_exit = session_state.get("allow_early_exit", True)
        # NẾU ĐANG Ở GIAI ĐOẠN CLOSING (CANDIDATE ASKS AI - REVERSE Q&A):
        if current_stage == InterviewStage.CLOSING:
            return await self._handle_closing_turn(
                turn_input=turn_input,
                session_state=session_state,
                is_vi=is_vi,
                time_remaining_seconds=time_remaining_seconds,
            )

        current_question = session_state.get("current_question_context", {})
        question_prompt = current_question.get("main_prompt", "")

        intent = classify_candidate_intent(candidate_text, question_prompt)

        # 2. ĐÁNH GIÁ CÂU TRẢ LỜI HIỆN TẠI (TURN EVALUATION)
        eval_result = await self._evaluate_candidate_response(
            candidate_text=turn_input.text_content,
            current_stage=current_stage,
            current_question=current_question,
            is_vi=is_vi,
            telemetry=turn_input.telemetry,
        )

        # BẢO HIỂM TẦNG 2: Nếu LLM nhận diện ý định xin dừng phỏng vấn -> Kích hoạt Modal xác nhận
        if eval_result.get("intent") == "CANDIDATE_ABORT":
            confirm_msg = (
                "Mình đã mở hộp thoại xác nhận kết thúc buổi phỏng vấn ngay bên dưới khung chat. Bạn có thể bấm xác nhận để hoàn tất phiên nhé."
                if is_vi
                else "I have opened the confirmation dialog to end the interview right below in this chat. Please confirm if you wish to conclude."
            )
            return InterviewerTurnOutput(
                session_id=session_id,
                turn_index=turn_input.turn_index + 1,
                message_text=confirm_msg,
                action=TurnAction.CONFIRM_ABORT,
                current_stage=current_stage,
                is_session_finished=False,
                metadata={
                    "requires_abort_confirmation": True,
                    "candidate_abort_intent": True,
                    "consecutive_uncooperative": 0,
                    "consecutive_fails": 0,  # Deprecated compatibility alias.
                },
            )

        # Abort precedence is resolved above. Clarify is allowed only when neither
        # deterministic nor evaluator intent indicates that the candidate wants to stop.
        clarify_count = session_state.get("clarify_count", 0)
        if intent == "CLARIFY_REQUEST" and current_stage not in (InterviewStage.WARM_UP, InterviewStage.CLOSING):
            if clarify_count < 1:
                clarify_msg = await self._generate_clarify_text(
                    current_question=current_question,
                    candidate_text=candidate_text,
                    is_vi=is_vi,
                )
                return InterviewerTurnOutput(
                    session_id=session_id,
                    turn_index=turn_input.turn_index + 1,
                    message_text=clarify_msg,
                    action=TurnAction.CLARIFY,
                    current_stage=current_stage,
                    time_remaining_seconds=time_remaining_seconds,
                    metadata={
                        "consecutive_uncooperative": 0,
                        "consecutive_fails": 0,  # Deprecated compatibility alias.
                        "turn_in_question": current_turn_in_question,
                        "clarify_used": True,
                        "sufficiency_status": "AMBIGUOUS",
                    },
                )

        # Gate 4 Architectural Invariant:
        # Numeric score is telemetry/evaluation-only; it has zero control-flow authority in Gate 4.
        score_telemetry = float(eval_result.get("score", 6.0))

        # Runtime control flow is driven strictly by semantic signals:
        # candidate intent, answer sufficiency, missing evidence, probe budget, time budget.
        sufficiency_status = eval_result.get("sufficiency_status")
        if not sufficiency_status:
            sufficiency_status = "SUFFICIENT" if eval_result.get("is_sufficient", True) else "INSUFFICIENT"
        is_sufficient = (sufficiency_status == "SUFFICIENT")

        is_give_up = (intent == "GIVE_UP")

        # Adaptive Probing: Nếu ứng viên trả lời quá ngắn (< 15 từ hoặc < 70 ký tự)
        # trong khi câu hỏi không phải là Yes/No và chưa từng probe ở câu này:
        words = candidate_text.split()
        is_too_brief = (
            len(words) < 15
            and len(candidate_text) < 70
            and not is_yes_no_question(question_prompt)
            and not is_give_up
        )
        if is_too_brief:
            is_sufficient = False
            sufficiency_status = "INSUFFICIENT"

        # 3. PO-approved Gate 4 counter policy: only explicit GIVE_UP increments
        # the canonical counter. Scores, insufficiency, honest answers, skip and
        # clarification have no authority to increment it.
        if intent == "GIVE_UP":
            consecutive_uncooperative += 1
        else:
            consecutive_uncooperative = 0
        session_state["consecutive_uncooperative"] = consecutive_uncooperative

        technical_stages = {InterviewStage.DEEP_DIVE, InterviewStage.CHALLENGE}
        if (
            allow_early_exit
            and current_stage in technical_stages
            and consecutive_uncooperative >= 2
        ):
            uncoop_msg = (
                "Cảm ơn bạn. Hệ thống đã ghi nhận đầy đủ các thông tin cần thiết cho vị trí này "
                "và xin phép kết thúc phiên phỏng vấn tại đây. Báo cáo đánh giá sẽ được gửi tới bạn qua email."
                if is_vi
                else "Thank you. We have recorded sufficient insights for this position and will end the interview here. "
                     "The evaluation report will be emailed to you."
            )
            return self._terminate_session(
                session_id=session_id,
                turn_index=turn_input.turn_index + 1,
                reason=SessionExitReason.FAST_FAIL_TECH,
                message=uncoop_msg,
                metadata={
                    "consecutive_uncooperative": consecutive_uncooperative,
                    "consecutive_fails": consecutive_uncooperative,
                },
            )

        # 4. ĐIỀU HÒA NHỊP ĐỘ (PACING OFFSET) & QUYẾT ĐỊNH PROBE
        # Probe Budget Guardrails
        closing_reserve_seconds = session_state.get(
            "closing_reserve_seconds", pacing_seconds(60, target_duration_seconds)
        )
        behavioral_reserve_seconds = session_state.get(
            "behavioral_reserve_seconds", pacing_seconds(180, target_duration_seconds)
        )
        hard_answer_seconds = current_question.get("hard_answer_seconds", 180)

        is_hard_answer_timeout = (
            turn_input.duration_seconds > hard_answer_seconds
            if turn_input.duration_seconds and turn_input.duration_seconds > 0
            else False
        )
        is_near_closing_reserve = time_remaining_seconds <= closing_reserve_seconds
        is_near_behavioral_reserve = (
            time_remaining_seconds <= (closing_reserve_seconds + behavioral_reserve_seconds)
            and current_stage in [InterviewStage.VALIDATE, InterviewStage.DEEP_DIVE, InterviewStage.CHALLENGE]
        )
        asked_ids = set(str(qid) for qid in session_state.get("asked_question_ids", []))
        behavioral_queue = session_state.get("questions_pool", {}).get(InterviewStage.BEHAVIORAL.value, [])
        has_pending_behavioral = any(
            str(q.get("question_id") or q.get("question_version_id") or "") not in asked_ids
            for q in behavioral_queue
        )

        # Nếu đang bị trễ giờ hơn 80% thời gian -> cấm probe, ép chuyển câu
        is_behind_schedule = (elapsed_seconds > (target_duration_seconds * 0.8)) and current_stage in [
            InterviewStage.VALIDATE,
            InterviewStage.DEEP_DIVE,
        ]

        should_probe = (
            # A demo asks each frozen question once so every stage fits.
            not session_state.get("is_demo")
            and not is_sufficient
            and not is_give_up
            and current_turn_in_question == 0
            and not is_behind_schedule
            and not is_hard_answer_timeout
            and not (is_near_behavioral_reserve and has_pending_behavioral)
            and not is_near_closing_reserve
            and current_stage in [
                InterviewStage.VALIDATE,
                InterviewStage.DEEP_DIVE,
                InterviewStage.CHALLENGE,
            ]
        )
        # Turn 0 (WARM_UP): Khóa tuyệt đối tính năng probe, luôn tiếp nhận chào hỏi và chuyển tiếp
        if current_stage == InterviewStage.WARM_UP:
            should_probe = False

        # 5. XỬ LÝ CHUYỂN BƯỚC FSM
        if should_probe:
            probe_question = await self._generate_probe_question(
                candidate_text=turn_input.text_content,
                current_question=current_question,
                is_vi=is_vi,
                telemetry=turn_input.telemetry,
                missing_aspect=eval_result.get("missing_aspect", ""),
            )
            return InterviewerTurnOutput(
                session_id=session_id,
                turn_index=turn_input.turn_index + 1,
                message_text=probe_question,
                action=TurnAction.PROBE,
                current_stage=current_stage,
                time_remaining_seconds=time_remaining_seconds,
                metadata={
                    "consecutive_uncooperative": consecutive_uncooperative,
                    "consecutive_fails": consecutive_uncooperative,  # Deprecated compatibility alias.
                    "turn_in_question": 1,
                    "pre_score": score_telemetry,
                    "sufficiency_status": sufficiency_status,
                    "hard_answer_timeout": is_hard_answer_timeout,
                },
            )

        # NẾU KHÔNG PROBE: CHUYỂN CÂU HỎI TIẾP THEO HOẶC ĐỔI STAGE
        next_stage, next_question = self._get_next_stage_and_question(session_state)

        if next_stage == InterviewStage.CLOSED or next_question is None:
            close_msg = (
                "Buổi phỏng vấn đã hoàn thành xuất sắc các nội dung. Cảm ơn bạn rất nhiều! "
                "Hệ thống đang tiến hành tổng hợp báo cáo đánh giá."
                if is_vi
                else "The interview has completed all sections successfully. Thank you very much! "
                     "The system is summarizing the evaluation report."
            )
            return self._terminate_session(
                session_id=session_id,
                turn_index=turn_input.turn_index + 1,
                reason=SessionExitReason.NORMAL_COMPLETION,
                message=close_msg,
            )

        default_ack = "Cảm ơn câu trả lời của bạn." if is_vi else "Thank you for your answer."
        interviewer_message = await self._synthesize_interviewer_speech(
            acknowledgement=eval_result.get("acknowledgement", default_ack),
            next_question_prompt=next_question.main_prompt,
            is_stage_transition=(next_stage != current_stage),
            is_vi=is_vi,
        )

        return InterviewerTurnOutput(
            session_id=session_id,
            turn_index=turn_input.turn_index + 1,
            message_text=interviewer_message,
            action=TurnAction.NEXT_QUESTION,
            current_stage=next_stage,
            current_competency=next_question.competency if next_question else None,
            time_remaining_seconds=time_remaining_seconds,
            metadata={
                "consecutive_uncooperative": consecutive_uncooperative,
                "consecutive_fails": consecutive_uncooperative,  # Deprecated compatibility alias.
                "turn_in_question": 0,
                "question_id": next_question.question_id if next_question else None,
                "pre_score": score_telemetry,
                "sufficiency_status": sufficiency_status,
                "hard_answer_timeout": is_hard_answer_timeout,
            },
        )

    # ----------------- CÁC HÀM BỔ TRỢ (HELPER METHODS) ----------------- #

    async def _handle_closing_turn(
        self,
        turn_input: CandidateTurnInput,
        session_state: Dict[str, Any],
        is_vi: bool,
        time_remaining_seconds: int,
    ) -> InterviewerTurnOutput:
        """Xử lý vòng Q&A ngược: ứng viên đặt câu hỏi cho phỏng vấn viên về công ty/văn hóa/dự án."""
        session_id = session_state.get("session_id", turn_input.session_id)
        candidate_text = turn_input.text_content.strip()
        job_title = session_state.get("job_title") or "Vị trí ứng tuyển"

        no_questions_patterns = [
            r"\b(không|k|hết|chưa)\b.*?\b(câu hỏi|thắc mắc|gì nữa|hỏi gì|hỏi thêm)\b",
            r"\b(cảm ơn|thank you|thanks)\b.*?\b(nhiều|bạn|anh|chị)?\b",
            r"\b(nắm rõ|hiểu rõ|đầy đủ|rõ rồi|oke|ok|dạ rồi)\b",
            r"\b(kết thúc|dừng|kết thúc tại đây)\b",
            r"\b(no|none|nothing)\b.*?\b(more\s+)?(questions?|else)\b",
            r"\b(end|finish|wrap up|stop)\b.*?\b(interview|here|now)\b",
            r"^(không|không ạ|dạ không|k ạ|hết rồi|dạ rõ rồi|ok|oke)$",
        ]
        # A message that asks something ("No worries, one question: ...?",
        # "Công ty có OT không? Cảm ơn ạ") is a question, even if it also
        # contains a polite closing phrase.
        has_no_more_q = "?" not in candidate_text and any(
            re.search(pat, candidate_text, re.IGNORECASE) for pat in no_questions_patterns
        )

        session_seconds = int(session_state.get("target_duration_minutes", 25) * 60)
        qna = session_state.get("closing_qna") or {}
        questions_asked = int(qna.get("questions_asked") or 1)
        qna_elapsed = int(qna.get("elapsed_seconds") or 0)
        # The Q&A used to run until the candidate stopped asking or the session
        # was 90s from its end, so it could take most of a session that
        # finished its assessment early. This is the last question we answer.
        is_demo = bool(session_state.get("is_demo"))
        is_last_question = questions_asked >= MAX_QNA_QUESTIONS or (
            not is_demo and qna_elapsed >= pacing_seconds(QNA_BUDGET_SECONDS, session_seconds)
        )
        if has_no_more_q or (not is_demo and time_remaining_seconds <= pacing_seconds(90, session_seconds)):
            farewell = (
                "Rất cảm ơn bạn đã tham gia buổi phỏng vấn hôm nay cùng INTERVIA! "
                "Chúc mừng bạn đã hoàn thành trọn vẹn tất cả các phần thi. "
                "Hệ thống đang tiến hành tổng hợp báo cáo đánh giá toàn diện năng lực của bạn. "
                "Chúc bạn một ngày làm việc thật nhiều năng lượng và thành công!"
                if is_vi
                else "Thank you very much for taking the time to interview with INTERVIA today! "
                     "Congratulations on completing all sections of the interview. "
                     "The system is now compiling your comprehensive evaluation report. "
                     "Wishing you a great day and all the best in your career!"
            )
            return self._terminate_session(
                session_id=session_id,
                turn_index=turn_input.turn_index + 1,
                reason=SessionExitReason.NORMAL_COMPLETION,
                message=farewell,
            )

        job_context = format_job_context(session_state.get("job_context"), is_vi)
        company = (session_state.get("job_context") or {}).get("company_name") or (
            "nhà tuyển dụng" if is_vi else "the employer"
        )
        remaining_minutes = max(1, time_remaining_seconds // 60)
        language = "tiếng Việt" if is_vi else "English"
        follow_up = (
            "Không hỏi thêm câu nào; chỉ trả lời câu hỏi, hệ thống sẽ tự chào kết thúc."
            if is_last_question
            else "Sau khi trả lời, hỏi ngắn gọn ứng viên còn câu hỏi nào khác không."
        )
        system_prompt = (
            f"Bạn là người phỏng vấn đại diện cho {company}, vị trí {job_title}.\n"
            f"Đây là phần ứng viên hỏi ngược; buổi phỏng vấn còn khoảng {remaining_minutes} phút.\n"
            "CHỈ được dùng thông tin trong phần THÔNG TIN TUYỂN DỤNG dưới đây. Nếu câu hỏi nằm ngoài "
            "các thông tin này (ví dụ OT, quy trình nội bộ, stack chưa nêu, phúc lợi chưa nêu), nói rõ "
            "rằng mô tả công việc chưa đề cập và bộ phận tuyển dụng sẽ trao đổi thêm. Tuyệt đối không bịa "
            "thông tin về công ty.\n"
            f"Trả lời bằng {language}, tối đa 4 câu. {follow_up}\n"
            "Tuyệt đối không chấm điểm hay nhận xét điểm số.\n\n"
            f"THÔNG TIN TUYỂN DỤNG:\n{job_context}"
        )
        user_content = f"Câu hỏi của ứng viên: {candidate_text}"

        try:
            if hasattr(self.llm, "generate_text"):
                answer = await self.llm.generate_text(system_prompt=system_prompt, user_content=user_content)
            else:
                answer = await self._generate_fn(
                    instructions=system_prompt,
                    input_text=user_content,
                    max_output_tokens=300,
                    temperature=0.4,
                )
            answer_text = str(answer).strip()
        except Exception as e:
            logger.warning(f"Error generating Q&A response in CLOSING stage: {e}")
            answer_text = (
                "Cảm ơn câu hỏi của bạn. Hiện mình chưa thể trả lời chi tiết nội dung này; "
                "bộ phận tuyển dụng sẽ trao đổi thêm với bạn."
                if is_vi
                else "Thank you for the question. I can't answer that in detail right now; "
                     "the recruiting team will follow up with you."
            )
            if not is_last_question:
                answer_text += (
                    "\n\nBạn còn câu hỏi nào khác không?" if is_vi else "\n\nDo you have any other questions?"
                )

        if is_last_question:
            closing_line = (
                "Cảm ơn bạn đã dành thời gian cho buổi phỏng vấn hôm nay. "
                "Hệ thống đang tổng hợp báo cáo đánh giá năng lực của bạn. Chúc bạn một ngày tốt lành!"
                if is_vi
                else "Thank you for your time today. The system is now compiling your evaluation report. "
                     "Have a great day!"
            )
            return self._terminate_session(
                session_id=session_id,
                turn_index=turn_input.turn_index + 1,
                reason=SessionExitReason.NORMAL_COMPLETION,
                message=f"{answer_text}\n\n{closing_line}",
            )

        return InterviewerTurnOutput(
            session_id=session_id,
            turn_index=turn_input.turn_index + 1,
            message_text=answer_text,
            action=TurnAction.NEXT_QUESTION,
            current_stage=InterviewStage.CLOSING,
            current_competency="Hỏi đáp & Văn hóa doanh nghiệp",
            time_remaining_seconds=time_remaining_seconds,
            metadata={
                "is_qna_followup": True,
                "turn_in_question": 1,
            },
        )

    def _terminate_session(
        self,
        session_id: UUID | str,
        turn_index: int,
        reason: SessionExitReason,
        message: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> InterviewerTurnOutput:
        return InterviewerTurnOutput(
            session_id=session_id,
            turn_index=turn_index,
            message_text=message,
            action=TurnAction.TERMINATE,
            current_stage=InterviewStage.CLOSED,
            is_session_finished=True,
            exit_reason=reason,
            time_remaining_seconds=0,
            metadata=metadata or {},
        )

    async def _evaluate_candidate_response(
        self,
        candidate_text: str,
        current_stage: InterviewStage,
        current_question: Dict[str, Any],
        is_vi: bool = True,
        telemetry: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Gọi LLM phân tích nhanh câu trả lời để quyết định probe & early-exit."""
        question_prompt = current_question.get("main_prompt", "")
        intent = classify_candidate_intent(candidate_text, question_prompt)

        # 1. HEURISTIC CỨNG: Phát hiện ứng viên bỏ cuộc / cộc lốc -> Gán 1.0 điểm, không gọi LLM
        if intent == "GIVE_UP":
            ack = (
                "Mình ghi nhận bạn chưa có câu trả lời cho phần này."
                if is_vi
                else "Noted that you do not have an answer for this section."
            )
            return {
                "intent": "GIVE_UP",
                "sufficiency_status": "INSUFFICIENT",
                "is_sufficient": False,
                "missing_aspect": "Candidate gave up",
                "acknowledgement": ack,
                "score": 1.0,  # telemetry-only; zero control authority
            }

        # 2. XỬ LÝ RIÊNG CHO BÀI THI CODING (INTERACTIVE CODE SANDBOX)
        is_coding = bool(
            telemetry
            and (
                telemetry.get("is_coding_turn")
                or telemetry.get("isCodingTurn")
                or telemetry.get("code_diff")
                or telemetry.get("codeDiff")
            )
        )
        if is_coding:
            test_passed = bool(telemetry.get("test_passed", telemetry.get("testPassed", False)))
            if test_passed:
                ack = (
                    "Giải pháp của bạn đã vượt qua tất cả test cases. Mình có một câu hỏi về giải pháp này."
                    if is_vi
                    else "Your solution passed all test cases. I have a follow-up question regarding your approach."
                )
                return {
                    "intent": "ANSWER",
                    "sufficiency_status": "INSUFFICIENT",  # Cố tình đặt INSUFFICIENT để kích hoạt vòng Probe
                    "is_sufficient": False,
                    "missing_aspect": "Deep dive into implementation choices",
                    "acknowledgement": ack,
                    "score": 8.5,  # telemetry-only; zero control authority
                }
            else:
                ack = (
                    "Mã nguồn hiện tại chưa vượt qua toàn bộ test cases."
                    if is_vi
                    else "The current code has not passed all test cases."
                )
                return {
                    "intent": "ANSWER",
                    "sufficiency_status": "INSUFFICIENT",
                    "is_sufficient": False,
                    "missing_aspect": "Test cases failing",
                    "acknowledgement": ack,
                    "score": 4.0,  # telemetry-only; zero control authority
                }

        system_prompt = """Bạn là chuyên gia phân tích kỹ thuật thời gian thực của hệ thống phỏng vấn INTERVIA.
Đọc câu trả lời của ứng viên đối chiếu với Câu hỏi gốc và Barem tiêu chí, trả về đúng 1 JSON duy nhất:
{
  "intent": "ANSWER" | "CANDIDATE_ABORT" | "SKIP_QUESTION" | "GIVE_UP",
  "sufficiency_status": "SUFFICIENT" | "INSUFFICIENT" | "AMBIGUOUS",
  "missing_aspect": "<mô tả ngắn gọn 1-2 khía cạnh cốt lõi mà ứng viên chưa làm rõ hoặc nêu sai, để trống nếu SUFFICIENT>",
  "acknowledgement": "<1 câu xác nhận ngắn, tự nhiên bằng tiếng Việt khoảng 3-8 từ (ví dụ 'Ok, mình hiểu rồi.'), tuyệt đối không khen ngợi quá mức, không lộ điểm số hay tiêu chí>",
  "score": <điểm từ 1.0 đến 10.0 (telemetry-only; zero control authority)>
}
Lưu ý quan trọng:
- Đánh giá khách quan, không thiên vị, không nịnh bợ (Anti-Sycophancy).
- Chấp nhận hiện tượng Code-Switching (chêm từ tiếng Anh chuyên ngành như API, Microservices, Cache, Scale, Deploy). Tuyệt đối không trừ điểm vì lý do này.
- Phỏng vấn thật nói ngắn: câu trả lời dạng liệt kê ý, gạch đầu dòng, câu cụt hay từ khóa kỹ thuật là BÌNH THƯỜNG. Đánh giá theo nội dung (đúng và đủ các ý cốt lõi của câu hỏi), KHÔNG theo văn phong hay độ dài. Không đặt INSUFFICIENT chỉ vì câu trả lời ngắn hoặc không thành đoạn văn.
- Chỉ đặt INSUFFICIENT khi thiếu hoặc sai ý cốt lõi, hoặc câu trả lời quá chung chung không cho thấy hiểu biết thực tế.
- Nếu ứng viên bày tỏ mong muốn dừng/nghỉ phỏng vấn, bận việc riêng/gia đình: đặt "intent": "CANDIDATE_ABORT".
- Nếu ứng viên xin đổi câu hỏi khác: đặt "intent": "SKIP_QUESTION".
- Nếu ứng viên bỏ cuộc hoặc không biết: đặt "intent": "GIVE_UP", sufficiency_status: "INSUFFICIENT".
- Nếu câu hỏi là dạng Yes/No hoặc khảo sát kinh nghiệm (Ví dụ: 'Bạn đã từng làm việc với Kubernetes chưa?') và ứng viên trả lời thành thật 'Chưa' hoặc 'Không', đặt "intent": "ANSWER", hãy ghi nhận câu trả lời chân thật đó một cách tôn trọng, đặt "sufficiency_status": "SUFFICIENT" thay vì coi là bỏ cuộc."""
        user_content = f"""Câu hỏi: {current_question.get('main_prompt', '')}
Tiêu chí đánh giá: {current_question.get('rubric_criteria', '')}
Giai đoạn: {current_stage.value}
Câu trả lời của ứng viên:
<candidate_response>
{candidate_text}
</candidate_response>"""

        try:
            if hasattr(self.llm, "generate_json"):
                res = await self.llm.generate_json(system_prompt=system_prompt, user_content=user_content)
                data = res if isinstance(res, dict) else json.loads(res)
            elif hasattr(self.llm, "generate_text"):
                raw = await self.llm.generate_text(system_prompt=system_prompt, user_content=user_content)
                data = json.loads(raw)
            else:
                raw = await self._generate_fn(
                    instructions=system_prompt,
                    input_text=user_content,
                    max_output_tokens=250,
                    temperature=0.1,
                )
                data = json.loads(raw)

            # Tương thích với format {"decision": "PROBE", "reply_text": "..."}
            if isinstance(data, dict) and "decision" in data:
                is_probe = data.get("decision") == "PROBE"
                return {
                    "intent": "ANSWER",
                    "sufficiency_status": "INSUFFICIENT" if is_probe else "SUFFICIENT",
                    "is_sufficient": not is_probe,
                    "missing_aspect": "",
                    "acknowledgement": "Cảm ơn câu trả lời của bạn." if is_vi else "Thank you for your response.",
                    "reply_text": data.get("reply_text", ""),
                    "score": 5.0 if is_probe else 8.0,  # telemetry-only
                }

            raw_status = data.get("sufficiency_status")
            if raw_status in ("SUFFICIENT", "INSUFFICIENT", "AMBIGUOUS"):
                sufficiency_status = raw_status
            elif "is_sufficient" in data:
                sufficiency_status = "SUFFICIENT" if data["is_sufficient"] else "INSUFFICIENT"
            else:
                sufficiency_status = "SUFFICIENT"
            is_sufficient = (sufficiency_status == "SUFFICIENT")

            return {
                "intent": str(data.get("intent", "ANSWER")),
                "sufficiency_status": sufficiency_status,
                "is_sufficient": is_sufficient,
                "missing_aspect": str(data.get("missing_aspect", "")),
                "acknowledgement": safe_acknowledgement(data.get("acknowledgement"), is_vi),
                # score is telemetry/evaluation-only; it has zero control-flow authority in Gate 4.
                "score": float(data.get("score", 6.0)),
            }
        except Exception as e:
            logger.warning(f"Error in evaluate_candidate_response: {e}")
            ack = "Cảm ơn chia sẻ của bạn." if is_vi else "Thank you for sharing."
            return {
                "intent": "ANSWER",
                "sufficiency_status": "INSUFFICIENT",
                "is_sufficient": False,
                "missing_aspect": "",
                "acknowledgement": ack,
                "score": 5.0,  # telemetry-only
            }

    async def _generate_probe_question(
        self,
        candidate_text: str,
        current_question: Dict[str, Any],
        is_vi: bool = True,
        telemetry: Optional[Dict[str, Any]] = None,
        missing_aspect: str = "",
    ) -> str:
        """Sinh câu hỏi đào sâu (Probe) duy nhất kèm guardrail chống rò rỉ rubric."""
        is_coding = bool(
            telemetry
            and (
                telemetry.get("is_coding_turn")
                or telemetry.get("isCodingTurn")
                or telemetry.get("code_diff")
                or telemetry.get("codeDiff")
            )
        )

        if is_coding:
            code_diff = (telemetry or {}).get("code_diff") or (telemetry or {}).get("codeDiff") or "N/A"
            test_passed = bool((telemetry or {}).get("test_passed", (telemetry or {}).get("testPassed", False)))
            system_prompt = (
                f"Bạn là Tech Lead {'người Việt Nam' if is_vi else ''} đang phỏng vấn ứng viên kỹ thuật.\n"
                "Ứng viên vừa nộp giải pháp sửa lỗi code cho bài toán.\n"
                f"Code Diff của họ là:\n{code_diff}\n"
                f"Kết quả Test Cases: {'ĐÃ VƯỢT QUA' if test_passed else 'CHƯA ĐẠT'}.\n"
                "Nhiệm vụ: Đóng vai Tech Lead, đưa ra 1 nhận xét ngắn gọn và đặt 1 câu hỏi phản biện sâu vào các đánh đổi kỹ thuật "
                "(ví dụ: tại sao dùng Lock thay vì Semaphore, cách phòng tránh Deadlock, hoặc độ phức tạp thời gian/bộ nhớ, xử lý ngoại lệ trong critical section).\n"
                "Tuyệt đối không đọc lại từng dòng code."
            )
            user_content = f"""Đề bài: {current_question.get('main_prompt', '')}
Lời giải thích của ứng viên: {candidate_text}
Hãy đưa ra nhận xét ngắn và 1 câu hỏi phản biện sâu:"""
            fallback_coding_probe = (
                "Giải pháp của bạn đã xử lý được vấn đề. Bạn có thể phân tích đánh đổi giữa việc dùng Lock so với Semaphore "
                "hoặc cách phòng tránh rủi ro Deadlock nếu có ngoại lệ phát sinh không?"
                if is_vi
                else "Your solution resolved the issue. Can you analyze the trade-offs between Lock vs Semaphore, "
                     "and how you would prevent Deadlocks if an exception is raised?"
            )
        else:
            system_prompt = (
                f"Bạn là một Tech Lead {'người Việt Nam' if is_vi else ''} phỏng vấn ứng viên kỹ sư phần mềm.\n"
                "Nhiệm vụ: Đặt ĐÚNG 1 CÂU HỎI ĐÀO SÂU (Follow-up / Probe) dựa trên câu trả lời của ứng viên và khía cạnh còn thiếu.\n"
                "CÁC NGUYÊN TẮC BẤT DI BẤT DỊCH:\n"
                "1. Độ dài: Đúng 1 câu hỏi ngắn (dưới 20 từ), hỏi đúng 1 ý, như người phỏng vấn nói miệng; trực diện vào giải pháp hoặc đánh đổi kỹ thuật (trade-offs, concurrency, failure handling).\n"
                "2. Ngôn ngữ: Tiếng Việt tự nhiên của dân công nghệ, giữ nguyên các thuật ngữ tiếng Anh gốc (ví dụ: Deadlock, Cache invalidation, Index, Latency). Không dùng tiếng Việt dịch thô.\n"
                "3. Nghiêm cấm rò rỉ barem: Tuyệt đối không chứa các từ ngữ liên quan đến 'điểm', 'thang điểm', 'rubric', 'tiêu chí', 'bạn chưa đạt điểm này'.\n"
                "4. Không bao bọc bằng lời chào hỏi rườm rà (ví dụ không thêm 'Chào bạn', 'Tôi muốn hỏi...'). Chỉ xuất trực tiếp nội dung câu hỏi."
            )
            missing_hint = f"\nKhía cạnh còn thiếu cần khoét sâu: {missing_aspect}" if missing_aspect else ""
            user_content = f"""Câu hỏi gốc: {current_question.get('main_prompt', '')}
Ứng viên vừa trả lời: {candidate_text}{missing_hint}
Hãy đưa ra câu hỏi probe:"""
            fallback_coding_probe = SAFE_FALLBACK_PROBE_VI if is_vi else SAFE_FALLBACK_PROBE_EN

        try:
            if hasattr(self.llm, "generate_text"):
                raw_probe = await self.llm.generate_text(system_prompt=system_prompt, user_content=user_content)
            else:
                raw_probe = await self._generate_fn(
                    instructions=system_prompt,
                    input_text=user_content,
                    max_output_tokens=150,
                    temperature=0.2,
                )
            cleaned = str(raw_probe).strip()
            # Trích xuất nếu LLM trả JSON dạng {"reply_text": "..."}
            try:
                probe_json = json.loads(cleaned)
                if isinstance(probe_json, dict) and "reply_text" in probe_json:
                    cleaned = str(probe_json["reply_text"]).strip()
            except Exception:
                pass
        except Exception as e:
            logger.warning(f"Error in generate_probe_question: {e}")
            trace_event(
                "interviewer",
                "probe_generation_error",
                error_type=type(e).__name__,
                error_message=str(e),
            )
            return fallback_coding_probe

        # Guardrail: kiểm tra pattern cấm
        if not validate_probe_text(cleaned):
            logger.warning("Probe text leaked prohibited pattern, falling back to safe probe.")
            trace_event(
                "interviewer",
                "probe_guardrail_fallback_triggered",
                reason="prohibited_pattern_leaked",
            )
            return fallback_coding_probe

        return cleaned if cleaned else fallback_coding_probe

    async def _synthesize_interviewer_speech(
        self,
        acknowledgement: str,
        next_question_prompt: str,
        is_stage_transition: bool,
        is_vi: bool = True,
    ) -> str:
        """Ghép câu ghi nhận ngắn với câu hỏi tiếp theo.

        No "now let's move on to the next section" filler: the stage stepper
        already shows the stage, and a real interviewer just asks the next thing.
        """
        return f"{acknowledgement}\n\n{next_question_prompt}"

    async def _generate_clarify_text(
        self,
        current_question: Dict[str, Any],
        candidate_text: str,
        is_vi: bool = True,
    ) -> str:
        """Giải thích / làm rõ câu hỏi khi ứng viên thắc mắc hoặc chưa hiểu đề, không lộ đáp án/rubric."""
        question_prompt = current_question.get("main_prompt", "")
        system_prompt = (
            f"Bạn là Phỏng vấn viên kỹ thuật {'người Việt Nam' if is_vi else ''}.\n"
            "Ứng viên chưa hiểu rõ câu hỏi hoặc nhờ làm rõ ý câu hỏi.\n"
            "Nhiệm vụ: Giải thích lại một cách ngắn gọn, rõ ràng trọng tâm câu hỏi trong 1-2 câu mà không tiết lộ đáp án, giải pháp mẫu hay tiêu chí chấm điểm.\n"
            "Giữ thái độ nhã nhặn, khích lệ ứng viên chia sẻ theo hiểu biết thực tế của họ."
        )
        user_content = f"Câu hỏi gốc: {question_prompt}\nThắc mắc của ứng viên: {candidate_text}"
        fallback = (
            f"Ý của câu hỏi là muốn tìm hiểu về: \"{question_prompt}\". Bạn hãy chia sẻ dựa trên trải nghiệm và kiến thức thực tế của mình nhé."
            if is_vi
            else f"The question is asking about: \"{question_prompt}\". Please feel free to share based on your actual experience."
        )
        try:
            if hasattr(self.llm, "generate_text"):
                resp = await self.llm.generate_text(system_prompt=system_prompt, user_content=user_content)
            else:
                resp = await self._generate_fn(
                    instructions=system_prompt,
                    input_text=user_content,
                    max_output_tokens=150,
                    temperature=0.2,
                )
            cleaned = str(resp).strip()
            if cleaned and validate_probe_text(cleaned):
                return cleaned
            return fallback
        except Exception as e:
            logger.warning(f"Error generating clarify text: {e}")
            return fallback

    def _get_next_stage_and_question(
        self,
        session_state: Dict[str, Any],
    ) -> Tuple[InterviewStage, Optional[QuestionItem]]:
        """Lấy câu hỏi tiếp theo từ Question Pool theo Dynamic Time-Budgeted Pacing.

        Pacing Precedence Hierarchy:
        1. Cutoff: time_remaining_seconds <= 90 -> stop issuing new questions.
        2. Behavioral reserve: transition only when a valid frozen Behavioral turn exists.
        3. Frozen queue availability and legacy ratio heuristics.
        4. Closing is reachable only after the Behavioral stage completes with >= 180s remaining.
        """
        current_stage = InterviewStage(session_state.get("current_stage", InterviewStage.WARM_UP))
        questions_pool = session_state.get("questions_pool", {})
        asked_ids = set(str(qid) for qid in session_state.get("asked_question_ids", []))
        answered_ids = set(str(qid) for qid in session_state.get("answered_question_ids", []))
        if not answered_ids and asked_ids:
            answered_ids = set(asked_ids)
        curr_qid = str((session_state.get("current_question_context") or {}).get("question_id") or "")
        if curr_qid:
            asked_ids.add(curr_qid)
            answered_ids.add(curr_qid)

        is_vi = (session_state.get("locale") or "vi").lower().startswith("vi")

        # 1. Tính toán ngân sách thời gian thực
        target_duration_minutes = session_state.get("target_duration_minutes", 25)
        target_duration_seconds = max(60, target_duration_minutes * 60)
        now = datetime.now(timezone.utc)
        started_at = session_state.get("started_at") or now
        if isinstance(started_at, str):
            try:
                started_at = datetime.fromisoformat(started_at)
            except Exception:
                started_at = now
        if started_at is None:
            started_at = now
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)

        if session_state.get("remaining_time") is not None:
            time_remaining_seconds = max(0, int(session_state["remaining_time"]))
            elapsed_seconds = max(0, target_duration_seconds - time_remaining_seconds)
        elif session_state.get("elapsed_time") is not None and session_state.get("elapsed_time") > 0:
            elapsed_seconds = int(session_state["elapsed_time"])
            time_remaining_seconds = max(0, target_duration_seconds - elapsed_seconds)
        else:
            elapsed_seconds = max(0, int((now - started_at).total_seconds()))
            time_remaining_seconds = max(0, target_duration_seconds - elapsed_seconds)
        elapsed_ratio = min(1.0, elapsed_seconds / target_duration_seconds)

        closing_reserve_seconds = session_state.get(
            "closing_reserve_seconds", pacing_seconds(60, target_duration_seconds)
        )
        behavioral_reserve_seconds = session_state.get(
            "behavioral_reserve_seconds", pacing_seconds(180, target_duration_seconds)
        )
        emergency_cutoff_seconds = pacing_seconds(90, target_duration_seconds)
        closing_min_seconds = pacing_seconds(180, target_duration_seconds)

        def as_valid_frozen_question(st: InterviewStage, q: Dict[str, Any]) -> Optional[QuestionItem]:
            """Return a well-formed frozen turn only when it belongs to the requested stage."""
            qid = str(q.get("question_id") or "")
            qvid = str(q.get("question_version_id") or "")
            if not (qid or qvid) or not str(q.get("main_prompt") or "").strip():
                return None
            try:
                declared_stage = InterviewStage(q.get("stage", st.value))
                if declared_stage != st:
                    return None
                q_data = dict(q)
                q_data["stage"] = st
                return QuestionItem(**q_data)
            except (TypeError, ValueError):
                return None

        # Helper lấy câu hỏi chưa hỏi, hợp lệ trong đúng stage của frozen queue.
        def pop_from_stage(st: InterviewStage) -> Optional[QuestionItem]:
            stage_qs = questions_pool.get(st.value, [])
            for q in stage_qs:
                qid = str(q.get("question_id") or "")
                qvid = str(q.get("question_version_id") or "")
                if (qid and qid in asked_ids) or (qvid and qvid in asked_ids):
                    continue
                question = as_valid_frozen_question(st, q)
                if question:
                    return question
            return None

        def has_pending_technical() -> bool:
            return (
                pop_from_stage(InterviewStage.VALIDATE) is not None
                or pop_from_stage(InterviewStage.DEEP_DIVE) is not None
                or pop_from_stage(InterviewStage.CHALLENGE) is not None
            )

        # A demo is turn-driven: no time cutoff, reserve jump or closing minimum,
        # so it walks every frozen stage (its hard timeout is the overtime grace).
        is_demo = bool(session_state.get("is_demo"))

        # 2. Emergency turn cutoff: do not issue a new turn with <= 90 seconds
        # left (scaled down for sessions shorter than 25 minutes).
        if not is_demo and time_remaining_seconds <= emergency_cutoff_seconds:
            trace_event(
                "interviewer",
                "closing_pacing_triggered",
                time_remaining_seconds=time_remaining_seconds,
                current_stage=current_stage.value,
            )
            # The behavioral (STAR) question is a required part of every
            # interview. A long technical answer used to land inside the cutoff
            # and close the session without ever asking it; ask it now while the
            # session is still above its hard timeout.
            if current_stage not in (InterviewStage.BEHAVIORAL, InterviewStage.CLOSING) and (
                time_remaining_seconds > pacing_seconds(30, target_duration_seconds)
            ):
                q_beh = pop_from_stage(InterviewStage.BEHAVIORAL)
                if q_beh:
                    return InterviewStage.BEHAVIORAL, q_beh
            return InterviewStage.CLOSED, None

        # 2b. Behavioral reserve may advance only to a valid frozen Behavioral turn.
        if (
            not is_demo
            and time_remaining_seconds <= (closing_reserve_seconds + behavioral_reserve_seconds)
            and current_stage in [InterviewStage.VALIDATE, InterviewStage.DEEP_DIVE, InterviewStage.CHALLENGE]
        ):
            trace_event(
                "interviewer",
                "behavioral_reserve_protection_triggered",
                time_remaining_seconds=time_remaining_seconds,
                current_stage=current_stage.value,
            )
            q_beh = pop_from_stage(InterviewStage.BEHAVIORAL)
            if q_beh:
                return InterviewStage.BEHAVIORAL, q_beh
            # No Behavioral turn exists: continue the remaining assessment queue.

        # 3. Điều phối theo Tỷ lệ Thời gian Thực (Dynamic Pacing Controller)
        if current_stage == InterviewStage.WARM_UP:
            # Warm-up hoàn thành 1 lượt -> Luôn chuyển sang VALIDATE
            q = pop_from_stage(InterviewStage.VALIDATE)
            if q:
                return InterviewStage.VALIDATE, q
            q = pop_from_stage(InterviewStage.DEEP_DIVE)
            if q:
                return InterviewStage.DEEP_DIVE, q

        elif current_stage == InterviewStage.VALIDATE:
            # Nếu chưa vượt ngưỡng 30% thời gian -> Ưu tiên kiểm tra tiếp Validate nếu còn câu
            if elapsed_ratio < TIME_THRESHOLDS[InterviewStage.VALIDATE]:
                q = pop_from_stage(InterviewStage.VALIDATE)
                if q:
                    return InterviewStage.VALIDATE, q
            # Đã xong Validate hoặc chạm mốc -> Chuyển sang DEEP_DIVE
            q = pop_from_stage(InterviewStage.DEEP_DIVE)
            if q:
                return InterviewStage.DEEP_DIVE, q
            q = pop_from_stage(InterviewStage.CHALLENGE)
            if q:
                return InterviewStage.CHALLENGE, q

        elif current_stage == InterviewStage.DEEP_DIVE:
            # Dynamic Loop: Còn thời gian (< 75%) -> Tiếp tục rút từ Queue DEEP_DIVE
            if elapsed_ratio < TIME_THRESHOLDS[InterviewStage.DEEP_DIVE]:
                q = pop_from_stage(InterviewStage.DEEP_DIVE)
                if q:
                    return InterviewStage.DEEP_DIVE, q
                # Nếu đã hết câu DEEP_DIVE trong pool mà còn nhiều giờ -> Rút tiếp CHALLENGE
                q = pop_from_stage(InterviewStage.CHALLENGE)
                if q:
                    return InterviewStage.CHALLENGE, q
            else:
                # Đã chạm hoặc vượt 75% -> Ưu tiên chuyển sang CHALLENGE, sau đó nốt DEEP_DIVE
                q = pop_from_stage(InterviewStage.CHALLENGE)
                if q:
                    return InterviewStage.CHALLENGE, q
                q = pop_from_stage(InterviewStage.DEEP_DIVE)
                if q:
                    return InterviewStage.DEEP_DIVE, q

            # Chỉ chuyển sang BEHAVIORAL khi toàn bộ frozen DEEP_DIVE và CHALLENGE đã cạn
            if not has_pending_technical():
                q = pop_from_stage(InterviewStage.BEHAVIORAL)
                if q:
                    return InterviewStage.BEHAVIORAL, q

        elif current_stage == InterviewStage.CHALLENGE:
            if elapsed_ratio < TIME_THRESHOLDS[InterviewStage.CHALLENGE]:
                q = pop_from_stage(InterviewStage.CHALLENGE)
                if q:
                    return InterviewStage.CHALLENGE, q
            # Rút tiếp CHALLENGE nếu còn
            q = pop_from_stage(InterviewStage.CHALLENGE)
            if q:
                return InterviewStage.CHALLENGE, q
            # Rút tiếp các câu DEEP_DIVE còn pending từ các competency khác
            q = pop_from_stage(InterviewStage.DEEP_DIVE)
            if q:
                return InterviewStage.DEEP_DIVE, q
            # Chỉ chuyển sang BEHAVIORAL khi toàn bộ frozen DEEP_DIVE và CHALLENGE đã cạn
            if not has_pending_technical():
                q = pop_from_stage(InterviewStage.BEHAVIORAL)
                if q:
                    return InterviewStage.BEHAVIORAL, q

        elif current_stage == InterviewStage.BEHAVIORAL:
            # Behavioral must be fully drained regardless of elapsed-ratio pacing.
            # Once in BEHAVIORAL, NEVER fall back to DEEP_DIVE or CHALLENGE.
            q = pop_from_stage(InterviewStage.BEHAVIORAL)
            if q:
                return InterviewStage.BEHAVIORAL, q

            behavioral_queue = questions_pool.get(InterviewStage.BEHAVIORAL.value, [])
            has_completed_behavioral = any(
                as_valid_frozen_question(InterviewStage.BEHAVIORAL, frozen_q) is not None
                and (
                    str(frozen_q.get("question_id") or "") in answered_ids
                    or str(frozen_q.get("question_version_id") or "") in answered_ids
                )
                for frozen_q in behavioral_queue
            )
            if not has_completed_behavioral:
                return InterviewStage.CLOSED, None

            # Behavioral is complete. Closing still requires at least 180 seconds
            # (scaled down for sessions shorter than 25 minutes).
            if is_demo or time_remaining_seconds >= closing_min_seconds:
                frozen_closing = pop_from_stage(InterviewStage.CLOSING)
                if frozen_closing:
                    return InterviewStage.CLOSING, frozen_closing
                prompt = (
                    f"Mình hỏi xong rồi. Bạn có câu hỏi nào về công việc, team hay công ty không? "
                    f"(tối đa {MAX_QNA_QUESTIONS} câu)"
                    if is_vi
                    else "That's all from me. Any questions about the role, the team or the company? "
                         f"(up to {MAX_QNA_QUESTIONS})"
                )
                return InterviewStage.CLOSING, QuestionItem(
                    question_id="closing-candidate-qna",
                    stage=InterviewStage.CLOSING,
                    competency="Hỏi đáp & Văn hóa doanh nghiệp",
                    main_prompt=prompt,
                )
            return InterviewStage.CLOSED, None

        # 4. Fallback: Duyệt tuần tự qua các stage còn lại
        stage_order = [
            InterviewStage.WARM_UP,
            InterviewStage.VALIDATE,
            InterviewStage.DEEP_DIVE,
            InterviewStage.CHALLENGE,
            InterviewStage.BEHAVIORAL,
        ]
        curr_idx = stage_order.index(current_stage) if current_stage in stage_order else 0
        for next_st in stage_order[curr_idx + 1:]:
            if next_st == InterviewStage.BEHAVIORAL and has_pending_technical():
                continue
            q = pop_from_stage(next_st)
            if q:
                return next_st, q

        # Exhausting an assessment queue is not evidence that Behavioral completed.
        # Only the BEHAVIORAL branch above may open Closing.
        return InterviewStage.CLOSED, None
