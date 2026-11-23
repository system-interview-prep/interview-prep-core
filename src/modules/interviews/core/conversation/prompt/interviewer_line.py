"""System prompt cho một lời thoại ngắn của người phỏng vấn."""

from __future__ import annotations

from src.modules.interviews.core.dialogue.policy import speech_prompt

__all__ = ["interviewer_line_prompt"]


def interviewer_line_prompt(*, is_vi: bool, purpose: str, max_words: int) -> str:
    """Prompt diễn đạt một hành động hội thoại thành lời nói tự nhiên."""
    language = "tiếng Việt" if is_vi else "English"
    return speech_prompt(
        is_vi=is_vi,
        role="Bạn là người phỏng vấn trực tiếp, đang phản hồi trong một cuộc trò chuyện.",
        task=f"Diễn đạt lời phản hồi cho hành động '{purpose}' dựa trên ngữ cảnh.",
        rules=(
            f"Dùng {language}, tối đa {max_words} từ.",
            "Nói tự nhiên, ngắn gọn, phù hợp với điều ứng viên vừa nói.",
            "Không nhắc điểm, barem hoặc tiêu chí nội bộ.",
            "Không tự đổi hành động của phiên hoặc đặt thêm câu hỏi ngoài yêu cầu.",
            "Chỉ trả lời phần lời thoại, không giải thích cách làm.",
        ),
        output="Lời thoại của người phỏng vấn.",
    )
