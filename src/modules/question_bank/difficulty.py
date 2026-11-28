"""What each difficulty band means, in one place and for every skill.

The band of a question used to be the author's own impression, and the quality
gate asked the judge "does it fit the difficulty?" without saying what the
difficulty was. These descriptors are skill-agnostic on purpose: a technology
that enters the taxonomy tomorrow is levelled with the same three definitions.

They line up with the seniority mapping the planner already uses
(``interviews/agents/planner/plan_structure.py``): intern, fresher and junior
are asked foundational questions, mid intermediate, senior and lead advanced.

Used by the generation prompt and its judge, the import template, and the
admin page (``GET /admin/question-bank/difficulty-guide``).
"""

from __future__ import annotations

from typing import Any

from src.modules.question_bank.schemas import DIFFICULTY_BANDS

GUIDE_VERSION = "1"

_GUIDE: dict[str, dict[str, dict[str, str]]] = {
    "foundational": {
        "vi": {
            "label": "Cơ bản",
            "audience": "Người mới học, chưa đi làm (thực tập, fresher, junior).",
            "expects": "Giải thích được khái niệm và lý do dùng nó.",
            "shape": "X là gì, khác Y thế nào, khi nào dùng?",
        },
        "en": {
            "label": "Foundational",
            "audience": "Someone new to the skill, without work experience (intern, fresher, junior).",
            "expects": "Explains the concept and why it is used.",
            "shape": "What is X, how does it differ from Y, when would you use it?",
        },
    },
    "intermediate": {
        "vi": {
            "label": "Trung cấp",
            "audience": "Người đã tự làm việc được với kỹ năng này (mid).",
            "expects": "Áp dụng vào một tình huống thực tế, nêu cách làm và lý do.",
            "shape": "Bạn xử lý thế nào khi gặp tình huống ...?",
        },
        "en": {
            "label": "Intermediate",
            "audience": "Someone who works with the skill independently (mid-level).",
            "expects": "Applies it to a realistic situation and gives the approach with a reason.",
            "shape": "How would you handle ... when it happens?",
        },
    },
    "advanced": {
        "vi": {
            "label": "Nâng cao",
            "audience": "Người có nhiều năm kinh nghiệm (senior, lead).",
            "expects": "So sánh phương án, nêu đánh đổi và rủi ro, chẩn đoán sự cố.",
            "shape": "Hệ thống gặp ... bạn chọn hướng nào, vì sao, rủi ro gì?",
        },
        "en": {
            "label": "Advanced",
            "audience": "Someone with years of experience (senior, lead).",
            "expects": "Compares options, names trade-offs and risks, diagnoses failures.",
            "shape": "The system shows ... which way do you go, why, and what can go wrong?",
        },
    },
}


def _language(locale: str | None) -> str:
    return "vi" if str(locale or "").lower().startswith("vi") else "en"


def difficulty_guide(locale: str | None = "vi-VN") -> list[dict[str, Any]]:
    """The three bands in asking order, worded for ``locale``."""
    language = _language(locale)
    return [{"band": band, **_GUIDE[band][language]} for band in DIFFICULTY_BANDS]


def difficulty_prompt_block() -> str:
    """The definitions as prompt text. English: the prompts around it are English."""
    lines = ["Difficulty levels (the same for every skill):"]
    for band in DIFFICULTY_BANDS:
        item = _GUIDE[band]["en"]
        lines.append(f'- {band}: {item["audience"]} {item["expects"]} Typical shape: "{item["shape"]}"')
    return "\n".join(lines)


def difficulty_summary(locale: str | None = "vi-VN") -> str:
    """One line per band, for a spreadsheet cell or a tooltip."""
    levels = difficulty_guide(locale)
    return "\n".join(f'{item["band"]} ({item["label"]}): {item["expects"]}' for item in levels)


__all__ = [
    "GUIDE_VERSION",
    "difficulty_guide",
    "difficulty_prompt_block",
    "difficulty_summary",
]
