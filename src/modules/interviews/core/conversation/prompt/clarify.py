"""System prompt nói lại câu hỏi khi ứng viên chưa hiểu đề."""

from __future__ import annotations

from src.modules.interviews.core.dialogue.policy import (
    speech_prompt,
    speech_v2_enabled,
    style_guide,
)

__all__ = ["clarify_prompt"]


def clarify_prompt(is_vi: bool) -> str:
    """Prompt diễn đạt lại câu hỏi, không lộ đáp án hay barem."""
    if is_vi:
        system_prompt = (
            "Bạn là người phỏng vấn kỹ thuật. Ứng viên chưa hiểu câu hỏi hoặc trả lời lạc đề.\n"
            "Nhiệm vụ: NÓI LẠI CÂU HỎI theo cách khác, dễ hiểu hơn, như đang nói chuyện.\n"
            "- Tối đa 2 câu, tổng dưới 40 từ, một đoạn, không xuống dòng.\n"
            "- Không nhận xét hay sửa lời ứng viên vừa nói; không dùng 'hãy', 'không cần', 'đừng', 'vui lòng'.\n"
            "- Có thể mở đầu nhẹ kiểu 'Ý mình là…' hoặc 'Mình hỏi theo cách khác nhé.'\n"
            "- Không tiết lộ đáp án, giải pháp mẫu hay tiêu chí chấm điểm.\n\n"
            + style_guide(is_vi)
            + "\n\nVí dụ: câu hỏi 'Kể một lần bạn bất đồng trong team' và ứng viên nói linh tinh → "
            "'Ý mình là một lần bạn và đồng đội không cùng ý kiến về công việc. Lúc đó bạn đã làm gì?'"
        )
    else:
        system_prompt = (
            "You are a technical interviewer. The candidate did not understand the question or went off track.\n"
            "Task: SAY THE QUESTION AGAIN in a different, simpler way, conversationally. At most two sentences, "
            "under 40 words, one paragraph. Do not comment on or correct what the candidate said; no 'you should', "
            "'don't', 'please'. Reveal no answer, model solution or scoring criteria.\n\n"
            + style_guide(is_vi)
        )
    if speech_v2_enabled():
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
                    "Tối đa 2 câu, tổng dưới 40 từ, một đoạn, không xuống dòng.",
                    "Không nhận xét hay sửa lời ứng viên; không dùng 'không cần', 'đừng'.",
                    "Không tiết lộ đáp án hay giải pháp mẫu.",
                )
                if is_vi
                else (
                    "At most two sentences, under 40 words, one paragraph.",
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
