"""Interviewer Agent capability to turn frozen written questions into speech.

This module has no database access. The application runtime owns loading and
persisting snapshots; this capability only drafts and validates question text.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from src.modules.ai.facade import generate_text
from src.modules.interviews.core.dialogue.policy import style_guide
from src.modules.interviews.core.guardrails.output import is_evaluative

GenerateFn = Callable[..., Awaitable[Any]]
MAX_SPOKEN_CHARS = 400
_WORD_RE = re.compile(r"[\w.+#\-]+")
_ASCII_TERM_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+#.\-]*$")
_ASCII_VI_WORDS = {
    "cho", "con", "khi", "hay", "theo", "trong", "sang", "nhanh", "nhau", "thay", "ban", "minh",
    "nao", "cac", "mot", "hai", "thi", "vao", "ra", "lam", "nhu", "tai", "sao", "ghi", "xem",
    "so", "la", "co", "de", "do", "di", "em", "anh", "ma", "ve", "ta", "ai", "gi", "ha", "chi", "nhe",
    "and", "or", "the", "a", "an", "of", "to", "in", "for", "with", "how", "what", "why", "you",
    "your", "is", "are", "does", "can", "would", "when", "which", "that", "this",
}
_BENIGN_SCORE_WORDS_RE = re.compile(r"\b(ưu nhược điểm|ưu điểm|nhược điểm|điểm mạnh|điểm yếu|điểm khác)\b", re.IGNORECASE)
_IMPERATIVE_OPENING_RE = re.compile(
    r"^\W*(?:hãy\s+)?(?:trình bày|giải thích|nêu|mô tả|phân tích|liệt kê|so sánh)\b", re.IGNORECASE
)
_TEMPLATE_WRAP_RE = re.compile(r"\bgiúp mình\b.*\bnhé\W*$", re.IGNORECASE)


def _tech_tokens(value: str) -> set[str]:
    tokens = set()
    for word in _WORD_RE.findall(str(value or "")):
        if _ASCII_TERM_RE.match(word.strip(".-")):
            token = word.strip(".-").casefold()
            if len(token) >= 2 and token not in _ASCII_VI_WORDS:
                tokens.add(token)
    return tokens


def _expected_text(item: Mapping[str, Any]) -> str:
    value = item.get("expected_text") or ""
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def validate_spoken_question(original: str, spoken: str, *, expected_text: str = "") -> bool:
    # Import lazily because turn_engine loads the interview core package.
    from src.modules.interviews.core.conversation.turn_engine import PROHIBITED_LEAK_PATTERNS

    original, spoken = str(original or "").strip(), str(spoken or "").strip()
    if not original or not spoken or spoken == original or "\n" in spoken or len(spoken) > MAX_SPOKEN_CHARS:
        return False
    if len(spoken) < 0.5 * len(original) or len(spoken) > max(1.6 * len(original), len(original) + 80):
        return False
    from difflib import SequenceMatcher

    leak_text = _BENIGN_SCORE_WORDS_RE.sub(" ", spoken)
    if any(re.search(pattern, leak_text, re.IGNORECASE) for pattern in PROHIBITED_LEAK_PATTERNS):
        return False
    if _IMPERATIVE_OPENING_RE.match(spoken) or _TEMPLATE_WRAP_RE.search(spoken):
        return False
    if SequenceMatcher(None, original.casefold(), spoken.casefold()).ratio() > 0.95:
        return False
    if is_evaluative(spoken):
        return False
    original_terms, spoken_terms = _tech_tokens(original), _tech_tokens(spoken)
    return original_terms <= spoken_terms and not (spoken_terms & (_tech_tokens(expected_text) - original_terms))


def _prompt(is_vi: bool) -> str:
    if is_vi:
        return (
            "Chuyển câu hỏi phỏng vấn kỹ thuật từ văn viết thành cách hỏi trực tiếp, tự nhiên. "
            "Giữ nguyên trọng tâm, phạm vi và mọi thuật ngữ tiếng Anh trong câu gốc; không thêm ý, ví dụ, "
            "gợi ý đáp án, tiêu chí chấm hay lời dẫn. Chỉ viết lại cách nói, không ghép thêm câu. "
            "Mỗi câu hỏi là một trọng tâm, tối đa hai câu ngắn; tránh mẫu 'giúp mình ... nhé'.\n\n"
            + style_guide(True)
            + '\n\nTrả về đúng JSON: {"items":[{"id":"...","spoken":"..."}]}'
        )
    return (
        "Rewrite each technical interview question as a natural direct spoken question. Preserve its scope and all technical terms. "
        "Add no hints, examples, evaluation criteria or lead-in; do not join another sentence. Keep one focus and at most two short sentences.\n\n"
        + style_guide(False)
        + '\n\nReturn exactly JSON: {"items":[{"id":"...","spoken":"..."}]}'
    )


def _parse(raw: Any) -> dict[str, str]:
    data = raw
    if isinstance(raw, str):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
        data = json.loads(cleaned)
    items = data.get("items") if isinstance(data, dict) else data
    return {
        str(item["id"]): str(item["spoken"]).strip()
        for item in (items or [])
        if isinstance(item, dict) and item.get("id") and item.get("spoken")
    }


async def rewrite_spoken_questions(
    items: Sequence[Mapping[str, Any]],
    *,
    locale: str,
    generate_fn: GenerateFn | None = None,
    timeout_seconds: float = 20.0,
) -> dict[str, str]:
    """Return only validated rewrites, keyed by turn/question ID."""
    if not items:
        return {}
    generate_fn = generate_fn or generate_text
    is_vi = str(locale or "vi").lower().startswith("vi")
    payload = [{"id": str(item["id"]), "question": str(item["question"])} for item in items]
    try:
        raw = await asyncio.wait_for(
            generate_fn(
                instructions=_prompt(is_vi),
                input_text=json.dumps(payload, ensure_ascii=False),
                max_output_tokens=2000,
                temperature=0.3,
                purpose="interviews.agent.interviewer.spoken_question",
            ),
            timeout=timeout_seconds,
        )
        rewrites = _parse(raw)
    except Exception:
        return {}
    accepted: dict[str, str] = {}
    for item in items:
        key = str(item["id"])
        spoken = rewrites.get(key, "")
        if validate_spoken_question(str(item["question"]), spoken, expected_text=_expected_text(item)):
            accepted[key] = spoken
    return accepted
