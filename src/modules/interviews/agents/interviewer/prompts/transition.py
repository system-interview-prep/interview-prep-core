"""User context for a natural transition between interview topics."""

from __future__ import annotations

__all__ = ["transition_context"]


def transition_context(
    *,
    previous_topic: str,
    next_topic: str,
    next_question: str,
    candidate_answer: str,
    after_struggle: bool,
    close_previous_thread: bool,
    acknowledgement: str = "",
    is_vi: bool = True,
) -> str:
    """Give the speech model context without prescribing a transition phrase."""
    if is_vi:
        prior_line = (
            f"Câu ghi nhận sẽ nói ngay trước lời chuyển: {acknowledgement.strip()}. "
            "Lời chuyển phải nối tiếp tự nhiên, không nhắc lại câu ghi nhận. "
            if acknowledgement.strip()
            else ""
        )
        return (
            prior_line
            + f"Chủ đề vừa trao đổi: {previous_topic or 'chủ đề hiện tại'}. "
            f"Ứng viên vừa nói: {str(candidate_answer or '').strip()[:500] or 'không có câu trả lời'}. "
            f"Chủ đề/câu hỏi sắp tới: {next_topic or 'chủ đề tiếp theo'}. "
            f"Câu hỏi kế tiếp: {next_question}. "
            f"Ứng viên vừa gặp khó khăn hoặc xin bỏ qua: {after_struggle}. "
            f"Cần khép lại ý trước khi chuyển: {close_previous_thread}. "
            "Nếu cần, nối hai chủ đề bằng một câu tự nhiên dựa trên điều ứng viên vừa nói. "
            "Không bắt buộc phải có câu dẫn; không tóm tắt hay chép lại câu trả lời. "
            "Câu hỏi kế tiếp sẽ xuất hiện ngay sau lời chuyển, nên đừng thông báo quy trình hoặc nói rằng sắp hỏi câu khác. "
            "Không nhắc tên stage, rubric, số câu còn lại; không khen, chê hay tự kết luận về chất lượng câu trả lời."
        )
    return (
        (
            f"Acknowledgement immediately before the transition: {acknowledgement.strip()}. "
            "Continue naturally without repeating that acknowledgement. "
            if acknowledgement.strip()
            else ""
        )
        + f"Topic just discussed: {previous_topic or 'the current topic'}. "
        f"The candidate said: {str(candidate_answer or '').strip()[:500] or 'no answer'}. "
        f"Next topic or question: {next_topic or 'the next topic'}. "
        f"Next question: {next_question}. "
        f"The candidate may have struggled or asked to skip: {after_struggle}. "
        f"The previous thread may need closure: {close_previous_thread}. "
        "If useful, connect the topics in one natural sentence grounded in what the candidate said. "
        "A transition sentence is optional; do not summarize or repeat the answer. "
        "The next question follows immediately, so do not announce the process or say that another question is coming. "
        "Do not mention stages, rubrics or remaining question counts, and do not praise, criticize or judge the answer."
    )
