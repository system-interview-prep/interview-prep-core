"""System prompt nói lại câu hỏi khi ứng viên chưa hiểu đề."""

from __future__ import annotations

from src.modules.interviews.core.dialogue.policy import (
    speech_prompt,
    style_guide,
)

__all__ = ["clarify_prompt"]


def clarify_prompt(is_vi: bool) -> str:
    """Prompt diễn đạt lại câu hỏi, không lộ đáp án hay barem."""
    system_prompt = speech_prompt(
        is_vi=is_vi,
        role=(
            "Bạn là người phỏng vấn kỹ thuật của Intervia. Ứng viên chưa hiểu câu hỏi hoặc trả lời lạc đề."
            if is_vi
            else "You are Intervia's technical interviewer. The candidate did not understand the question."
        ),
        task=(
            "NÓI LẠI CÂU HỎI theo cách khác, dễ hiểu hơn, như đang nói chuyện."
            if is_vi
            else "SAY THE QUESTION AGAIN in a simpler way, conversationally."
        ),
        rules=(
            (
                "Dùng một hoặc hai câu ngắn và giải thích vừa đủ để câu hỏi rõ nghĩa.",
                "Không nhận xét hay sửa lời ứng viên; tránh giọng ra lệnh.",
                "Không tiết lộ đáp án hay giải pháp mẫu.",
            )
            if is_vi
            else (
                "Use one or two short sentences and enough context to make the question clear.",
                "Do not correct the candidate.",
            )
        ),
        examples=(
            (
                "Câu hỏi 'Kể một lần bạn bất đồng trong team', ứng viên nói linh tinh → 'Ý mình là một lần bạn "
                "và đồng đội không cùng ý kiến về công việc. Lúc đó bạn đã làm gì?'",
            )
            if is_vi
            else ()
        ),
        output="Chỉ câu nói lại câu hỏi." if is_vi else "Only the restated question.",
    )
    return system_prompt
