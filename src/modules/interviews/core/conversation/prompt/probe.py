"""System prompt sinh câu hỏi đào sâu (probe), bản code và bản hội thoại."""

from __future__ import annotations

from src.modules.interviews.core.dialogue.policy import (
    candidate_block,
    speech_prompt,
    speech_v2_enabled,
    style_guide,
)

__all__ = ["coding_probe_prompt", "probe_prompt"]


def coding_probe_prompt(*, is_vi: bool, code_diff: str, test_passed: bool) -> str:
    """Prompt phản biện lời giải code vừa nộp."""
    system_prompt = (
        f"Bạn là Tech Lead {'người Việt Nam' if is_vi else ''} đang phỏng vấn ứng viên kỹ thuật.\n"
        "Ứng viên vừa nộp giải pháp sửa lỗi code cho bài toán.\n"
        f"Code Diff của họ là:\n{candidate_block(str(code_diff), tag='candidate_code')}\n"
        f"Kết quả Test Cases: {'ĐÃ VƯỢT QUA' if test_passed else 'CHƯA ĐẠT'}.\n"
        "Nhiệm vụ: Đóng vai Tech Lead, đưa ra 1 nhận xét ngắn gọn và đặt 1 câu hỏi phản biện sâu vào các đánh đổi kỹ thuật "
        "(ví dụ: tại sao dùng Lock thay vì Semaphore, cách phòng tránh Deadlock, hoặc độ phức tạp thời gian/bộ nhớ, xử lý ngoại lệ trong critical section).\n"
        "Tuyệt đối không đọc lại từng dòng code."
    )
    if speech_v2_enabled():
        # The old task ("đưa ra 1 nhận xét ngắn gọn") invited praise or a
        # verdict on the code; the acknowledgement is scripted now.
        system_prompt = speech_prompt(
            is_vi=is_vi,
            role="Bạn là người phỏng vấn kỹ thuật của Intervia. Ứng viên vừa nộp lời giải một bài code.",
            task=(
                "Đặt ĐÚNG 1 câu hỏi ngắn dưới 20 từ về đánh đổi kỹ thuật trong cách họ làm "
                "(độ phức tạp, xử lý lỗi, trường hợp biên, đồng thời).\n"
                f"Code diff của họ:\n{candidate_block(str(code_diff), tag='candidate_code')}\n"
                f"Kết quả test: {'đã qua hết' if test_passed else 'còn test chưa qua'}."
            ),
            rules=(
                "Không nhận xét lời giải, không đọc lại code.",
                "Không câu dẫn: hệ thống đã thêm một câu dẫn phía trước.",
            ),
            output="Chỉ câu hỏi.",
        )
    return system_prompt


def probe_prompt(is_vi: bool) -> str:
    """Prompt hỏi đào sâu đúng một ý còn thiếu trong câu trả lời."""
    system_prompt = (
        f"Bạn là một Tech Lead {'người Việt Nam' if is_vi else ''} phỏng vấn ứng viên kỹ sư phần mềm.\n"
        "Nhiệm vụ: Đặt ĐÚNG 1 CÂU HỎI ĐÀO SÂU (Follow-up / Probe) dựa trên câu trả lời của ứng viên và khía cạnh còn thiếu.\n"
        "CÁC NGUYÊN TẮC BẤT DI BẤT DỊCH:\n"
        "1. Độ dài: Đúng 1 câu hỏi ngắn (dưới 20 từ), hỏi đúng 1 ý, như người phỏng vấn nói miệng; trực diện vào giải pháp hoặc đánh đổi kỹ thuật (trade-offs, concurrency, failure handling).\n"
        "2. Ngôn ngữ: Tiếng Việt tự nhiên của dân công nghệ, giữ nguyên các thuật ngữ tiếng Anh gốc (ví dụ: Deadlock, Cache invalidation, Index, Latency). Không dùng tiếng Việt dịch thô.\n"
        "3. Nghiêm cấm rò rỉ barem: Tuyệt đối không chứa các từ ngữ liên quan đến 'điểm', 'thang điểm', 'rubric', 'tiêu chí', 'bạn chưa đạt điểm này'.\n"
        "4. Không bao bọc bằng lời chào hỏi rườm rà (ví dụ không thêm 'Chào bạn', 'Tôi muốn hỏi...'). Chỉ xuất trực tiếp nội dung câu hỏi "
        "(hệ thống đã tự thêm một câu ghi nhận ngắn phía trước).\n\n" + style_guide(is_vi)
    )
    if speech_v2_enabled():
        system_prompt = speech_prompt(
            is_vi=is_vi,
            role=(
                "Bạn là người phỏng vấn kỹ thuật của Intervia, đang phỏng vấn trực tiếp ứng viên."
                if is_vi
                else "You are Intervia's technical interviewer, interviewing the candidate in person."
            ),
            task=(
                "Đặt ĐÚNG 1 câu hỏi đào sâu về khía cạnh còn thiếu trong câu trả lời: giải pháp, đánh đổi, "
                "xử lý lỗi hoặc đồng thời."
                if is_vi
                else "Ask EXACTLY one follow-up question on what the answer left out: the approach, a "
                "trade-off, failure handling or concurrency."
            ),
            rules=(
                (
                    "Một câu hỏi dưới 20 từ, hỏi đúng một ý, như nói miệng.",
                    "Không câu dẫn, không chào: hệ thống đã thêm một câu ghi nhận phía trước.",
                    "Giữ thuật ngữ tiếng Anh gốc (deadlock, index, latency), không dịch thô.",
                )
                if is_vi
                else (
                    "One question under 20 words, one point, as spoken.",
                    "No preamble or greeting: an acknowledgement is added before it.",
                )
            ),
            examples=(
                (
                    "Khi Redis lỗi, hệ thống của bạn xử lý thế nào?",
                    "Vì sao bạn chọn cursor thay cho offset ở đây?",
                )
                if is_vi
                else ("What happens when Redis is down?",)
            ),
            output="Chỉ câu hỏi, không JSON, không dấu ngoặc kép." if is_vi else "Only the question.",
        )
    return system_prompt
