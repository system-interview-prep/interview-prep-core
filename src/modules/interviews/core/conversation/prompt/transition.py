"""Ngữ cảnh gửi cho model khi chuyển sang chủ đề/câu hỏi kế tiếp.

Đây là phần user content của một lời thoại chuyển ý, không phải system
prompt, nhưng là văn bản hướng dẫn model nên để cạnh các prompt khác.
"""

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
) -> str:
    """Mô tả cho model biết vừa hỏi gì, sắp hỏi gì và được phép nói tới đâu."""
    return (
        f"Ứng viên vừa trả lời xong một câu{f' về {previous_topic}' if previous_topic else ''}. "
        f"Ý chính trong câu trả lời vừa rồi: {str(candidate_answer or '').strip()[:500] or 'không có'}. "
        f"Chuyện bạn sắp hỏi tới: {next_topic or 'một chuyện khác'}. "
        f"Câu hỏi kế tiếp: {next_question}. "
        f"Ứng viên có thể vừa xin bỏ qua hoặc gặp khó khăn: {after_struggle}. "
        f"Đóng chủ đề/câu hỏi vừa rồi trước khi chuyển: {close_previous_thread}. "
        "Nếu chuyển câu hoặc chủ đề, hãy ghi nhận ngắn gọn điều ứng viên vừa nói và báo rằng bạn sẽ hỏi sang chuyện khác. "
        "Có thể diễn đạt linh hoạt như 'Được rồi, mình nắm phần đó. Giờ mình muốn hỏi bạn về…', không chép nguyên câu mẫu. "
        "Buổi phỏng vấn được sắp xếp thế nào là chuyện của riêng bạn: không gọi tên phần, vòng, giai đoạn hay bước nào, "
        "không nói về quy trình hay kế hoạch, không cho biết còn bao nhiêu câu, và không xin ứng viên xác nhận hay đồng ý. "
        "Không làm ứng viên tưởng bạn vẫn hỏi tiếp chủ đề cũ; không khen/chê, chấm điểm hay tiết lộ đánh giá."
    )
