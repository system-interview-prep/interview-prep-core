"""System prompt cho vòng ứng viên hỏi ngược ở cuối buổi phỏng vấn."""

from __future__ import annotations

from src.modules.interviews.core.dialogue.policy import speech_prompt

__all__ = ["closing_qna_speech_prompt"]


def closing_qna_speech_prompt(
    *,
    is_vi: bool,
    job_title: str,
    company: str,
    remaining_minutes: int,
    job_context: str,
    is_last_question: bool = False,
) -> str:
    """One complete candidate-facing answer, including its next-turn ending."""
    language = "tiếng Việt" if is_vi else "English"
    ending = (
        "Sau khi trả lời, khép lại buổi phỏng vấn tự nhiên ngay trong cùng lời thoại."
        if is_last_question and is_vi
        else "After answering, close the interview naturally in this same response."
        if is_last_question
        else "Sau khi trả lời, nếu phù hợp, mời ứng viên hỏi thêm bằng một câu trần thuật ngắn; không đặt câu hỏi."
        if is_vi
        else "After answering, invite the candidate to ask more in one brief statement; do not ask a question."
    )
    return speech_prompt(
        is_vi=is_vi,
        role=(
            "Bạn là người phỏng vấn của Intervia, đang ở phần ứng viên hỏi ngược "
            f"về vị trí {job_title} tại {company}. Buổi phỏng vấn còn khoảng {remaining_minutes} phút."
        ),
        task=(
            (
                "Trả lời câu hỏi của ứng viên CHỈ bằng THÔNG TIN TUYỂN DỤNG bên dưới. Nếu câu hỏi nằm ngoài "
                "các thông tin này (ví dụ OT, quy trình nội bộ, stack chưa nêu, phúc lợi chưa nêu), nói rõ "
                "rằng mô tả công việc chưa đề cập và bộ phận tuyển dụng sẽ trao đổi thêm."
            )
            if is_vi
            else (
                "Answer the candidate using only the JOB INFORMATION below. If the information is not there "
                "(for example overtime, internal process, an unstated stack or benefits), say the posting does "
                "not specify it and the recruiting team can clarify. Do not invent company information."
            )
        ),
        rules=(
            f"Trả lời bằng {language}, một đoạn, tối đa 3 câu.",
            ending,
            "Không hỏi lại nội dung vừa được trả lời và không khen câu hỏi.",
            "Không đưa con số, tên công ty hay công nghệ không có trong thông tin tuyển dụng.",
        ),
        output=f"Một lời thoại hoàn chỉnh, không chờ code ghép thêm câu.\n\nTHÔNG TIN TUYỂN DỤNG:\n{job_context}",
    )
