"""Wording of the generic rubric every seed question shares, in Vietnamese.

Every session runs in vi-VN and the grader's prompt is Vietnamese, but the
three seeded criteria and their anchors were written in English, so the admin
page and the grader both read "Relevance: Answer addresses the question ...".

``SEED_CRITERIA`` is what new seed questions get. ``localize_seed_rubrics``
brings an existing database along: the seed inserts rubrics with
``ON CONFLICT DO NOTHING``, so changing the text here alone never reaches rows
that already exist. The update is guarded by the old English wording, so a
criterion or anchor somebody edited is left exactly as they wrote it, and a
second run changes nothing. Weights, keys and levels are untouched: this is a
translation, not a new rubric version. Sessions already planned keep the
rubric frozen in their question snapshot.
"""
# ruff: noqa: E501

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# The rubric versions the seed creates (question_bank_seed._SEED_AUTHOR); a rubric
# a person wrote is never touched, whatever its wording.
SEED_AUTHOR = "system-seed"

# ``en_name`` / ``en_description`` are the wording existing databases still hold.
SEED_CRITERIA: list[dict[str, Any]] = [
    {
        "stable_key": "relevance",
        "id_namespace": "criterion-relevance",
        "weight": Decimal("0.35"),
        "critical": False,
        "display_order": 1,
        "name": "Đúng trọng tâm",
        "description": "Câu trả lời đi thẳng vào câu hỏi và đầy đủ.",
        "en_name": "Relevance",
        "en_description": "Answer addresses the question directly and completely.",
    },
    {
        "stable_key": "technical_depth",
        "id_namespace": "criterion-depth",
        "weight": Decimal("0.35"),
        "critical": True,
        "display_order": 2,
        "name": "Chiều sâu kỹ thuật",
        "description": "Thể hiện kiến thức kỹ thuật chính xác và chi tiết.",
        "en_name": "Technical Depth",
        "en_description": "Demonstrates accurate, detailed technical knowledge.",
    },
    {
        "stable_key": "clarity",
        "id_namespace": "criterion-clarity",
        "weight": Decimal("0.30"),
        "critical": False,
        "display_order": 3,
        "name": "Rõ ràng",
        "description": "Giải thích rõ ràng, có cấu trúc và dễ theo dõi.",
        "en_name": "Clarity",
        "en_description": "Explanation is clear, structured, and easy to follow.",
    },
]

# criterion stable_key -> anchors for levels 0..3: (old English description, description, positive, negative)
SEED_ANCHORS: dict[str, list[tuple[str, str, list[str], list[str]]]] = {
    "relevance": [
        ("Off-topic or does not address the question.", "Lạc đề hoặc không trả lời câu hỏi.", [], ["Bỏ qua câu hỏi"]),
        ("Partially addresses the question with gaps.", "Trả lời được một phần, còn thiếu ý.", ["Có cố gắng trả lời"], ["Thiếu ý chính"]),
        ("Addresses the question adequately.", "Trả lời đúng và đủ ý chính của câu hỏi.", ["Nêu được các ý chính"], []),
        ("Comprehensively addresses all aspects.", "Trả lời trọn vẹn mọi khía cạnh của câu hỏi.", ["Bao quát đầy đủ", "Có thêm chiều sâu liên quan"], []),
    ],
    "technical_depth": [
        ("No meaningful technical content.", "Không có nội dung kỹ thuật đáng kể.", [], ["Không có chi tiết kỹ thuật"]),
        ("Basic technical understanding only.", "Chỉ hiểu ở mức cơ bản.", ["Biết khái niệm bề mặt"], ["Không giải thích được cơ chế"]),
        ("Solid technical knowledge with correct detail.", "Kiến thức kỹ thuật vững, chi tiết chính xác.", ["Dùng đúng thuật ngữ", "Giải thích được cơ chế"], []),
        ("Expert-level technical depth with nuance.", "Chiều sâu kỹ thuật ở mức chuyên gia, có phân tích tinh tế.", ["Nhận biết đánh đổi", "Xử lý trường hợp biên", "Có kinh nghiệm thực tế"], []),
    ],
    "clarity": [
        ("Incoherent or extremely difficult to follow.", "Rời rạc hoặc rất khó theo dõi.", [], ["Không theo dõi được lời giải thích"]),
        ("Some structure but hard to follow in places.", "Có cấu trúc nhưng đôi chỗ khó theo dõi.", ["Có cố gắng sắp xếp ý"], ["Mất mạch trình bày"]),
        ("Clear and reasonably well-structured.", "Rõ ràng và có cấu trúc hợp lý.", ["Mạch lạc"], []),
        ("Exceptionally clear, concise, and structured.", "Rất rõ ràng, súc tích và chặt chẽ.", ["Cấu trúc xuất sắc", "Dùng ví dụ so sánh hiệu quả"], []),
    ],
}


def seed_anchor_records() -> dict[str, list[dict[str, Any]]]:
    """Anchors in the shape the seed inserts: level, description, positive, negative."""
    return {
        key: [
            {"level": level, "description": description, "positive": positive, "negative": negative}
            for level, (_english, description, positive, negative) in enumerate(anchors)
        ]
        for key, anchors in SEED_ANCHORS.items()
    }


async def localize_seed_rubrics(db: AsyncSession) -> dict[str, int]:
    """Translate seeded criteria and anchors that still carry the original English wording.

    Returns how many rows changed. Flushes nothing itself; the caller commits.
    """
    criteria = anchors = 0
    for criterion in SEED_CRITERIA:
        key = criterion["stable_key"]
        for level, (english, description, positive, negative) in enumerate(SEED_ANCHORS[key]):
            result = await db.execute(
                text(
                    "UPDATE rubric_anchors a SET description = :description, "
                    "positive_indicators = CAST(:positive AS jsonb), negative_indicators = CAST(:negative AS jsonb) "
                    "FROM rubric_criteria c JOIN rubric_versions rv ON rv.id = c.rubric_version_id "
                    "WHERE a.criterion_id = c.id AND c.stable_key = :key AND rv.created_by = :author "
                    "AND a.level = :level AND a.description = :english"
                ),
                {
                    "description": description,
                    "positive": json.dumps(positive, ensure_ascii=False),
                    "negative": json.dumps(negative, ensure_ascii=False),
                    "key": key,
                    "author": SEED_AUTHOR,
                    "level": level,
                    "english": english,
                },
            )
            anchors += result.rowcount or 0
        result = await db.execute(
            text(
                "UPDATE rubric_criteria c SET name = :name, description = :description "
                "FROM rubric_versions rv WHERE rv.id = c.rubric_version_id AND rv.created_by = :author "
                "AND c.stable_key = :key AND c.name = :en_name AND c.description = :en_description"
            ),
            {
                "name": criterion["name"],
                "description": criterion["description"],
                "author": SEED_AUTHOR,
                "key": key,
                "en_name": criterion["en_name"],
                "en_description": criterion["en_description"],
            },
        )
        criteria += result.rowcount or 0
    return {"criteria": criteria, "anchors": anchors}
