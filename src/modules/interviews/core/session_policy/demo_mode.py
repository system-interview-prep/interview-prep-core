"""Phiên demo: gói ngắn dùng để trình bày trực tiếp mọi giai đoạn phỏng vấn.

Một phiên demo tuân theo số lượt cố định thay vì đồng hồ. Với nhịp độ thông
thường dựa trên thời gian, một phiên 3 phút chỉ đủ cho một câu hỏi kỹ thuật,
nên không bao giờ đạt đến CHALLENGE, đồng thời độ trễ của LLM đã sử dụng hết
thời gian dự phòng dành cho phần Hỏi & Đáp cuối. Bộ lập kế hoạch, bộ chọn câu
hỏi và công cụ lõi đều đọc mô-đun này để quy tắc được duy trì tại một nơi.
"""

# Gói "Demo" của frontend (DEMO_DURATION_MINUTES trong questionBudget.ts).
DEMO_DURATION_MINUTES = 3

# Các câu hỏi kỹ thuật cố định cho demo: một DEEP_DIVE và một CHALLENGE.
DEMO_TECHNICAL_QUESTIONS = 2

# Demo có thể vượt quá thời lượng danh nghĩa này trước khi thời gian chờ cứng
# đóng phiên, để người trình bày vẫn đạt đến BEHAVIORAL và phần Hỏi & Đáp cuối
# khi dán câu trả lời với nhịp độ bình thường.
DEMO_OVERTIME_GRACE_SECONDS = 180


def is_demo_duration(duration_minutes: int | float | None) -> bool:
    try:
        return 0 < float(duration_minutes or 0) <= DEMO_DURATION_MINUTES
    except (TypeError, ValueError):
        return False
