"""System prompt sinh câu hỏi đào sâu (probe), bản code và bản hội thoại."""

from __future__ import annotations

from src.modules.interviews.core.dialogue.policy import (
    speech_prompt,
    style_guide,
)
from src.modules.interviews.core.guardrails.common import candidate_block

__all__ = ["coding_probe_prompt", "probe_prompt"]


def coding_probe_prompt(*, is_vi: bool, code_diff: str, test_passed: bool) -> str:
    """Prompt phản biện lời giải code vừa nộp."""

    # The old task ("đưa ra 1 nhận xét ngắn gọn") invited praise or a
    # verdict on the code; the output is a standalone spoken question.
    system_prompt = speech_prompt(
        is_vi=is_vi,
        role=(
            "Bạn là người phỏng vấn kỹ thuật của Intervia. Ứng viên vừa nộp lời giải một bài code."
            if is_vi
            else "You are Intervia's technical interviewer. The candidate has submitted a coding solution."
        ),
        task=(
            (
                "Đặt một câu hỏi rõ ràng về một đánh đổi kỹ thuật trong cách họ làm; giữ câu hỏi gọn nhưng đủ ngữ cảnh.\n"
                f"Code diff của họ:\n{candidate_block(str(code_diff), tag='candidate_code')}\n"
                f"Kết quả test: {'đã qua hết' if test_passed else 'còn test chưa qua'}."
            )
            if is_vi
            else (
                "Ask one clear question about a technical trade-off in their solution. Keep it concise but clear.\n"
                f"Their code diff:\n{candidate_block(str(code_diff), tag='candidate_code')}\n"
                f"Test result: {'all passed' if test_passed else 'some tests failed or did not run'}."
            )
        ),
        rules=(
            (
                "Không nhận xét lời giải, không đọc lại code.",
                "Chỉ xuất câu hoàn chỉnh cần nói; hệ thống không nối thêm câu khác.",
            )
            if is_vi
            else (
                "Do not evaluate the solution or read the code back.",
                "Return the complete line to say; the system will not append another sentence.",
            )
        ),
        output="Chỉ câu hỏi." if is_vi else "Return only the question.",
    )
    return system_prompt


def probe_prompt(is_vi: bool) -> str:
    """Prompt hỏi đào sâu đúng một ý còn thiếu trong câu trả lời."""

    system_prompt = speech_prompt(
        is_vi=is_vi,
        role=(
            "Bạn là người phỏng vấn kỹ thuật của Intervia, đang phỏng vấn trực tiếp ứng viên."
            if is_vi
            else "You are Intervia's technical interviewer, interviewing the candidate in person."
        ),
        task=(
            "Viết lượt hỏi tiếp như người phỏng vấn đang trò chuyện thoải mái: nối vào một chi tiết cụ thể "
            "ứng viên vừa nói, rồi hỏi ĐÚNG 1 câu về khía cạnh còn thiếu của câu hỏi gốc. Ứng viên phải thấy "
            "câu hỏi này tiếp nối điều họ vừa kể, không phải một câu hỏi mới. Không dựng tình huống mới (tải "
            "tăng đột biến, sự cố, đánh đổi…) nếu ứng viên chưa nhắc và khía cạnh còn thiếu không đòi hỏi. "
            "Không mở bằng 'Cảm ơn bạn…'; không lặp cách mở của các lượt gần đây, nhất là khuôn "
            "'Bạn vừa nhắc đến…, vậy…'."
            if is_vi
            else "Write the next turn as an interviewer in conversation: pick up one specific thing the "
            "candidate just said (you may open with a short phrase naming it), then ask ONE question about "
            "what the original question still lacks. It must read as a continuation of what they said, not "
            "a new question. Do not invent a new scenario (load spikes, failures, trade-offs) unless the "
            "candidate raised it or the missing aspect requires it. Do not open with 'Thanks…', and do not "
            "reuse the opening of recent turns, especially 'You mentioned X, so…'."
        ),
        rules=(
            (
            "Hỏi đúng một ý trong một câu rõ ràng, như đang trò chuyện. Giữ câu hỏi gọn nhưng đủ ngữ cảnh.",
                "Chỉ xuất lời thoại hoàn chỉnh cần nói; hệ thống không nối thêm câu khác.",
                "Giữ thuật ngữ tiếng Anh gốc (deadlock, index, latency), không dịch thô.",
            )
            if is_vi
            else (
                "Ask one focused question in natural spoken language. Keep it concise but clear.",
                "Return only the complete line to say; the system will not append another sentence.",
            )
        ),
        examples=(
            (
                "Ừm, ok. À, mình muốn hỏi thêm một ý: repository đó bạn tự thiết kế hay theo mẫu có sẵn của nhóm?",
                "À, mình hiểu rồi. Vậy còn 401 với 403 thì sao, trong dự án có chỗ nào bạn phải phân biệt hai mã đó không?",
                "Chỗ phân trang ấy, sao bạn chọn cursor mà không dùng offset?",
            )
            if is_vi
            else (
                "Hmm, okay. Oh, one more thing: did you design that repository layer yourself or follow the team's template?",
                "Why cursor rather than offset there?",
            )
        ),
        output="Chỉ câu hỏi, không JSON, không dấu ngoặc kép." if is_vi else "Only the question.",
    )
    return system_prompt
