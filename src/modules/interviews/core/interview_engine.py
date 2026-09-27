# src/modules/interviews/core/interview_engine.py
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple
from uuid import UUID

from src.modules.ai.facade import generate_text
from src.modules.interviews.core.interview_types import (
    CandidateTurnInput,
    InterviewerTurnOutput,
    InterviewStage,
    QuestionItem,
    SessionExitReason,
    TurnAction,
)

logger = logging.getLogger("InterviewCoreEngine")

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


ABORT_PATTERNS = [
    # Mẫu 1: Xin dừng / xin nghỉ / xin out / xin rút / xin thôi
    r"(xin|cho|cho phép|muốn|phải|em|mình)\s*(phép\s*)?(dừng|nghỉ|out|thôi|rút|hủy|kết thúc)\b",
    # Mẫu 2: Có việc bận / việc gia đình / việc riêng đi kèm dừng/nghỉ
    r"(việc\s*(bận|gấp|đột xuất|gia đình|riêng)|bận\s*(rồi|quá|việc|gia đình))\b.*?\b(dừng|nghỉ|thôi|out|về|đi|kết thúc)",
    r"\b(dừng|nghỉ|thôi|out)\b.*?\b(việc\s*(bận|gấp|gia đình|riêng)|bận)",
    # Mẫu 3: Dừng lại đây / không phỏng vấn nữa
    r"(dừng\s*(lại\s*)?(đây|ở đây|tại đây|nhé|nha|ạ|thôi)|không\s*(phỏng vấn|tiếp tục|thi|làm)\s*nữa)",
    # Mẫu 4: Tiếng Anh
    r"\b(stop|end|abort|quit|cancel)\s+(the\s+)?(interview|session|meeting)\b",
    r"\b(want to|have to|need to|must|please)\s+(stop|quit|leave|exit|abort)\b",
    r"\b(cannot continue|can't continue|family emergency|stop here)\b",
]

SKIP_QUESTION_PATTERNS = [
    r"(cho\s*(em|mình)\s*)?(xin\s*)?(đổi|qua|bỏ qua|chuyển|skip)\s*(sang\s+)?(câu|câu hỏi|chủ đề|phần)",
    r"(câu\s*này|phần\s*này)\s*(em|mình)?\s*(chưa|không)\s*(rõ|biết|làm)\b.*?\b(câu khác|chủ đề khác|qua)",
    r"\b(next question|skip question|pass this|skip this question)\b",
]


def is_abort_request(text: str) -> bool:
    """Nhận diện mọi biến thể xin dừng phỏng vấn của ứng viên bằng Fuzzy Semantic Regex."""
    cleaned = text.strip().lower()
    return any(re.search(pattern, cleaned, re.IGNORECASE) for pattern in ABORT_PATTERNS)


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
        if is_abort_request(candidate_text):
            confirm_msg = (
                "Mình đã mở hộp thoại xác nhận kết thúc buổi phỏng vấn. Bạn vui lòng kiểm tra và xác nhận nhé."
                if is_vi
                else "I have opened the confirmation dialog to end the interview. Please check and confirm."
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
                },
            )

        now = datetime.now(timezone.utc)

        # 1. TÍNH TOÁN THỜI LƯỢNG & PACING
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

        elapsed_seconds = max(0, int((now - started_at).total_seconds()))
        target_duration_seconds = session_state.get("target_duration_minutes", 25) * 60
        time_remaining_seconds = max(0, target_duration_seconds - elapsed_seconds)

        # Kiểm tra Hard Timeout toàn phiên (<= 30 giây và chưa ở stage CLOSING)
        current_stage = InterviewStage(session_state.get("current_stage", InterviewStage.WARM_UP))
        if time_remaining_seconds <= 30 and current_stage != InterviewStage.CLOSING:
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
            consecutive_fails = session_state.get("consecutive_fails", 0)
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
                    "consecutive_fails": min(consecutive_fails + 1, 2),
                    "turn_in_question": 0,
                    "pre_score": 2.0,
                },
            )

        consecutive_fails = session_state.get("consecutive_fails", 0)
        current_turn_in_question = session_state.get("current_turn_in_question", 0)  # 0: Câu chính, 1: Câu probe
        allow_early_exit = session_state.get("allow_early_exit", True)
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
                "Mình đã mở hộp thoại xác nhận kết thúc buổi phỏng vấn. Bạn vui lòng kiểm tra và xác nhận nhé."
                if is_vi
                else "I have opened the confirmation dialog to end the interview. Please check and confirm."
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
                },
            )

        score = float(eval_result.get("score", 6.0))
        is_failed_answer = score < 4.0
        is_sufficient = bool(eval_result.get("is_sufficient", True))

        is_give_up = (intent == "GIVE_UP")

        # 3. KIỂM TRA ĐIỀU KIỆN DỪNG SỚM (2-STRIKE SYSTEM)
        if is_failed_answer:
            consecutive_fails += 1
        else:
            consecutive_fails = 0  # Reset nếu trả lời đạt

        if allow_early_exit and consecutive_fails >= 2:
            # Quy tắc 1: Trượt liên tiếp 2 lần ở vòng Validate CV (Nghi vấn CV ảo)
            if current_stage == InterviewStage.VALIDATE:
                msg = (
                    "Cảm ơn bạn đã tham gia buổi phỏng vấn hôm nay. Dựa trên các thông tin đã trao đổi, "
                    "hệ thống xin phép được hoàn tất vòng sơ loại tại đây. Kết quả chi tiết sẽ được gửi về email của bạn."
                    if is_vi
                    else "Thank you for joining today. Based on our conversation, we will conclude the screening round here. "
                         "Detailed results will be sent to your email."
                )
                return self._terminate_session(
                    session_id=session_id,
                    turn_index=turn_input.turn_index + 1,
                    reason=SessionExitReason.FAST_FAIL_VALIDATION,
                    message=msg,
                )
            # Quy tắc 2: Trượt 2 câu hỏi kỹ thuật tiên quyết hoặc bỏ cuộc liên tiếp
            else:
                msg = (
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
                    message=msg,
                )

        # 4. ĐIỀU HÒA NHỊP ĐỘ (PACING OFFSET) & QUYẾT ĐỊNH PROBE
        # Nếu đang bị trễ giờ hơn 80% thời gian -> cấm probe, ép chuyển câu
        is_behind_schedule = (elapsed_seconds > (target_duration_seconds * 0.8)) and current_stage in [
            InterviewStage.VALIDATE,
            InterviewStage.DEEP_DIVE,
        ]

        should_probe = (
            not is_sufficient
            and not is_give_up
            and current_turn_in_question == 0
            and not is_behind_schedule
            and consecutive_fails < 2
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
            )
            return InterviewerTurnOutput(
                session_id=session_id,
                turn_index=turn_input.turn_index + 1,
                message_text=probe_question,
                action=TurnAction.PROBE,
                current_stage=current_stage,
                time_remaining_seconds=time_remaining_seconds,
                metadata={
                    "consecutive_fails": consecutive_fails,
                    "turn_in_question": 1,
                    "pre_score": score,
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
                "consecutive_fails": consecutive_fails,
                "turn_in_question": 0,
                "question_id": next_question.question_id if next_question else None,
                "pre_score": score,
            },
        )

    # ----------------- CÁC HÀM BỔ TRỢ (HELPER METHODS) ----------------- #

    def _terminate_session(
        self,
        session_id: UUID | str,
        turn_index: int,
        reason: SessionExitReason,
        message: str,
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
                "score": 1.0,
                "is_sufficient": False,
                "acknowledgement": ack,
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
                    "score": 8.5,
                    "is_sufficient": False,  # Cố tình đặt False để kích hoạt vòng Probe phản biện giải pháp
                    "acknowledgement": ack,
                }
            else:
                ack = (
                    "Mã nguồn hiện tại chưa vượt qua toàn bộ test cases."
                    if is_vi
                    else "The current code has not passed all test cases."
                )
                return {
                    "score": 4.0,
                    "is_sufficient": False,
                    "acknowledgement": ack,
                }

        system_prompt = """Bạn là trợ lý đánh giá kỹ thuật thời gian thực.
Đọc câu trả lời của ứng viên cho câu hỏi phỏng vấn, trả về JSON duy nhất:
{
  "intent": "ANSWER" | "CANDIDATE_ABORT" | "SKIP_QUESTION" | "GIVE_UP",
  "score": <điểm từ 1.0 đến 10.0>,
  "is_sufficient": <true nếu đã đủ ý, false nếu câu trả lời quá ngắn, né tránh hoặc thiếu chi tiết then chốt>,
  "acknowledgement": "<1 câu chuyển ý ngắn gọn lịch sự ghi nhận câu trả lời của ứng viên, văn phong tự nhiên>"
}
Lưu ý quan trọng:
- Nếu ứng viên bày tỏ mong muốn dừng/nghỉ phỏng vấn, bận việc riêng/gia đình: đặt "intent": "CANDIDATE_ABORT".
- Nếu ứng viên xin đổi câu hỏi khác: đặt "intent": "SKIP_QUESTION".
- Nếu ứng viên bỏ cuộc hoặc không biết: đặt "intent": "GIVE_UP".
- Nếu câu hỏi là dạng Yes/No hoặc khảo sát kinh nghiệm (Ví dụ: 'Bạn đã từng làm việc với Kubernetes chưa?') và ứng viên trả lời thành thật 'Chưa' hoặc 'Không', đặt "intent": "ANSWER", hãy ghi nhận câu trả lời chân thật đó một cách tôn trọng, cho mức điểm trung tính hợp lý (5.0 - 6.0) thay vì coi là bỏ cuộc."""
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
                    "score": 5.0 if is_probe else 8.0,
                    "is_sufficient": not is_probe,
                    "acknowledgement": "Cảm ơn câu trả lời của bạn." if is_vi else "Thank you for your response.",
                    "reply_text": data.get("reply_text", ""),
                }

            return {
                "intent": str(data.get("intent", "ANSWER")),
                "score": float(data.get("score", 6.0)),
                "is_sufficient": bool(data.get("is_sufficient", True)),
                "acknowledgement": str(data.get("acknowledgement", "Cảm ơn bạn.")),
            }
        except Exception as e:
            logger.warning(f"Error in evaluate_candidate_response: {e}")
            ack = "Cảm ơn chia sẻ của bạn." if is_vi else "Thank you for sharing."
            if is_yes_no_question(question_prompt):
                fallback_score = 5.0
            else:
                fallback_score = 2.0 if len(candidate_text.strip()) < 20 else 5.0
            return {"intent": "ANSWER", "score": fallback_score, "is_sufficient": False, "acknowledgement": ack}

    async def _generate_probe_question(
        self,
        candidate_text: str,
        current_question: Dict[str, Any],
        is_vi: bool = True,
        telemetry: Optional[Dict[str, Any]] = None,
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
                f"Bạn là Tech Lead {'người Việt Nam' if is_vi else ''} đang phỏng vấn ứng viên.\n"
                "Nhiệm vụ: Đặt đúng 1 câu hỏi đào sâu (follow-up/probe) dựa trên những gì ứng viên vừa nói.\n"
                "Yêu cầu:\n"
                "1. Giao tiếp tự nhiên, giữ nguyên thuật ngữ tiếng Anh kỹ thuật (Deploy, Redis, Microservices, Scale...).\n"
                "2. Tuyệt đối không nhắc tới barem, điểm số, tiêu chí chấm thi hay rubric.\n"
                "3. Không hỏi lan man sang chủ đề khác, chỉ xoáy sâu vào cơ chế thực hiện hoặc lý do lựa chọn."
            )
            user_content = f"""Câu hỏi gốc: {current_question.get('main_prompt', '')}
Ứng viên vừa trả lời: {candidate_text}
Hãy đưa ra 1 câu hỏi đào sâu trực diện:"""
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
            return fallback_coding_probe

        # Guardrail: kiểm tra pattern cấm
        if not validate_probe_text(cleaned):
            logger.warning("Probe text leaked prohibited pattern, falling back to safe probe.")
            return fallback_coding_probe

        return cleaned if cleaned else fallback_coding_probe

    async def _synthesize_interviewer_speech(
        self,
        acknowledgement: str,
        next_question_prompt: str,
        is_stage_transition: bool,
        is_vi: bool = True,
    ) -> str:
        """Ghép câu chuyển ý và câu hỏi tiếp theo một cách tự nhiên."""
        if is_stage_transition:
            transition = "Bây giờ, chúng ta sẽ chuyển sang phần tiếp theo nhé." if is_vi else "Now, let's move on to the next section."
            return f"{acknowledgement} {transition}\n\n{next_question_prompt}"
        return f"{acknowledgement}\n\n{next_question_prompt}"

    def _get_next_stage_and_question(
        self,
        session_state: Dict[str, Any],
    ) -> Tuple[InterviewStage, Optional[QuestionItem]]:
        """Lấy câu hỏi tiếp theo từ Question Bank đã freeze theo FSM."""
        stage_order = [
            InterviewStage.WARM_UP,
            InterviewStage.VALIDATE,
            InterviewStage.DEEP_DIVE,
            InterviewStage.CHALLENGE,
            InterviewStage.BEHAVIORAL,
            InterviewStage.CLOSING,
            InterviewStage.CLOSED,
        ]
        current_stage = InterviewStage(session_state.get("current_stage", InterviewStage.WARM_UP))
        questions_pool = session_state.get("questions_pool", {})
        asked_ids = set(session_state.get("asked_question_ids", []))

        # Lấy câu hỏi chưa dùng trong stage hiện tại
        stage_questions = questions_pool.get(current_stage.value, [])
        for q in stage_questions:
            q_id = q.get("question_id") or q.get("question_version_id")
            if q_id not in asked_ids:
                q_data = dict(q)
                if "stage" not in q_data:
                    q_data["stage"] = current_stage
                return current_stage, QuestionItem(**q_data)

        # Nếu đã hết câu hỏi ở stage hiện tại -> Chuyển sang stage kế tiếp
        curr_idx = stage_order.index(current_stage)
        for next_stage in stage_order[curr_idx + 1:]:
            if next_stage == InterviewStage.CLOSED:
                return InterviewStage.CLOSED, None

            next_stage_questions = questions_pool.get(next_stage.value, [])
            for q in next_stage_questions:
                q_id = q.get("question_id") or q.get("question_version_id")
                if q_id not in asked_ids:
                    q_data = dict(q)
                    if "stage" not in q_data:
                        q_data["stage"] = next_stage
                    return next_stage, QuestionItem(**q_data)

        return InterviewStage.CLOSED, None
