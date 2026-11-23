"""System prompt cho vòng ứng viên hỏi ngược ở cuối buổi phỏng vấn."""

from __future__ import annotations

from src.modules.interviews.core.dialogue.policy import speech_prompt

__all__ = ["closing_qna_prompt", "closing_qna_speech_prompt"]


def closing_qna_prompt(
    *,
    company: str,
    job_title: str,
    remaining_minutes: int,
    language: str,
    follow_up: str,
    job_context: str,
) -> str:
    """Prompt trả lời câu hỏi của ứng viên, chỉ dựa trên thông tin tuyển dụng."""
    return (
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


def closing_qna_speech_prompt(
    *,
    is_vi: bool,
    job_title: str,
    company: str,
    remaining_minutes: int,
    job_context: str,
) -> str:
    """Bản speech v2 của prompt trên: một đoạn, tối đa ba câu."""
    language = "tiếng Việt" if is_vi else "English"
    return speech_prompt(
        is_vi=is_vi,
        role=(
            "Bạn là người phỏng vấn của Intervia, đang ở phần ứng viên hỏi ngược "
            f"về vị trí {job_title} tại {company}. Buổi phỏng vấn còn khoảng {remaining_minutes} phút."
        ),
        task=(
            "Trả lời câu hỏi của ứng viên CHỈ bằng THÔNG TIN TUYỂN DỤNG bên dưới. Nếu câu hỏi nằm ngoài "
            "các thông tin này (ví dụ OT, quy trình nội bộ, stack chưa nêu, phúc lợi chưa nêu), nói rõ "
            "rằng mô tả công việc chưa đề cập và bộ phận tuyển dụng sẽ trao đổi thêm."
        ),
        rules=(
            f"Trả lời bằng {language}, một đoạn, tối đa 3 câu.",
            "Không hỏi lại ứng viên, không khen câu hỏi; hệ thống tự hỏi ứng viên còn câu hỏi nào không.",
            "Không đưa con số, tên công ty hay công nghệ không có trong thông tin tuyển dụng.",
        ),
        output=f"Chỉ câu trả lời.\n\nTHÔNG TIN TUYỂN DỤNG:\n{job_context}",
    )
