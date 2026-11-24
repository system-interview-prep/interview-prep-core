"""System prompt cho một lời thoại ngắn của người phỏng vấn."""

from __future__ import annotations

from src.modules.interviews.core.dialogue.policy import speech_prompt

__all__ = ["interviewer_line_prompt"]


def interviewer_line_prompt(*, is_vi: bool, purpose: str, max_words: int) -> str:
    """Prompt diễn đạt một hành động hội thoại thành lời nói tự nhiên."""
    language = "tiếng Việt" if is_vi else "English"
    return speech_prompt(
        is_vi=is_vi,
        role=(
            "Bạn là người phỏng vấn trực tiếp, đang phản hồi trong một cuộc trò chuyện."
            if is_vi
            else "You are an interviewer responding in a live conversation."
        ),
        task=(
            f"Diễn đạt lời phản hồi cho hành động '{purpose}' dựa trên ngữ cảnh."
            if is_vi
            else f"Phrase the response for the action '{purpose}' using the conversation context."
        ),
        rules=(
            (
                f"Dùng {language}. Cố gắng gói gọn trong khoảng {max_words} từ nếu nội dung cho phép; ưu tiên câu nói tự nhiên, rõ ý.",
                "Phản hồi tự nhiên và cụ thể theo ngữ cảnh; không dùng một câu mẫu cho mọi tình huống.",
                "Không nhắc điểm, barem hoặc tiêu chí nội bộ.",
                "Không tự đổi hành động của phiên hoặc đặt thêm câu hỏi ngoài yêu cầu.",
                "Chỉ trả lời phần lời thoại, không giải thích cách làm.",
            )
            if is_vi
            else (
                f"Use English. Aim for around {max_words} words when the context allows, while keeping the line natural and clear.",
                "Respond specifically to the context instead of reusing one template for every situation.",
                "Do not mention scores, rubrics or internal criteria.",
                "Do not change the interview action or add a question that was not requested.",
                "Return only the spoken line, with no explanation.",
            )
        ),
        output="Lời thoại của người phỏng vấn." if is_vi else "The interviewer's spoken line.",
    )
