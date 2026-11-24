"""Fixed wording used only to open an interview across Chat, Voice, and Video.

This module contains only the common opening and first warm-up question.
"""

# Câu nói để nguyên một dòng cho dễ đọc và dễ sửa.
# ruff: noqa: E501

from __future__ import annotations

# --------------------------------------------------------------------------- #
# Mở đầu buổi (giống nhau cho chat, voice, video)
# --------------------------------------------------------------------------- #

OPENING_GREETING_VI = "Chào {name}, mình là người phỏng vấn AI của Intervia."
OPENING_GREETING_EN = "Hi {name}, I'm the AI interviewer from Intervia."
# {role}: " vị trí Backend Developer" hoặc rỗng; {length}: ", khoảng 25 phút" hoặc rỗng.
OPENING_FRAME_VI = "Hôm nay mình luyện phỏng vấn{role}{length}."
OPENING_FRAME_EN = "Today we'll practise an interview{role}{length}."
OPENING_ROLE_VI = " vị trí {job_title}"
OPENING_ROLE_EN = " for the {job_title} role"
OPENING_LENGTH_VI = ", khoảng {minutes} phút"
OPENING_LENGTH_EN = ", for about {minutes} minutes"
OPENING_AGENDA_VI = "Mình sẽ hỏi về {agenda}."
OPENING_AGENDA_EN = "I'll ask about {agenda}."
OPENING_START_VI = "Mình bắt đầu nhé."
OPENING_START_EN = "Let's begin."
OPENING_DEMO_VI = "Buổi demo ba phút, mình hỏi nhanh vài câu."
OPENING_DEMO_EN = "This is a three-minute demo, so I'll ask a few quick questions."

# Tên từng phần trong lời giới thiệu (WARM_UP là câu hỏi ngay sau; CLOSING báo riêng).
STAGE_PHRASES_VI: dict[str, str] = {
    "VALIDATE": "dự án trong CV",
    "DEEP_DIVE": "chuyên môn",
    "CHALLENGE": "vài tình huống",
    "BEHAVIORAL": "cách bạn làm việc nhóm",
}
STAGE_PHRASES_EN: dict[str, str] = {
    "VALIDATE": "a project from your CV",
    "DEEP_DIVE": "technical knowledge",
    "CHALLENGE": "a few scenarios",
    "BEHAVIORAL": "how you work with a team",
}

# Câu hỏi đầu tiên. Dự án trong CV được hỏi ngay sau (VALIDATE) nên không hỏi ở đây.
WARMUP_QUESTION_VI = (
    "Bạn giới thiệu ngắn về bản thân trong một, hai phút: "
    "bạn đang làm gì và vì sao quan tâm vị trí này?"
)
WARMUP_QUESTION_DEMO_VI = (
    "Bạn giới thiệu ngắn về bản thân trong khoảng ba mươi giây: "
    "hiện bạn đang làm gì và vì sao quan tâm vị trí này?"
)
WARMUP_QUESTION_EN = (
    "Please introduce yourself in a minute or two: "
    "what you do now and why this role interests you?"
)
WARMUP_QUESTION_DEMO_EN = (
    "Please introduce yourself in about thirty seconds: "
    "what you do now and why this role interests you?"
)
