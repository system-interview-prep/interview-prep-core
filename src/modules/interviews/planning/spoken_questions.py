"""Bản nói của câu hỏi: viết lại câu hỏi trong kho thành văn nói, một lần lúc khoá.

Câu hỏi trong ngân hàng được viết để đọc ("Trình bày cơ chế cache invalidation
trong Redis"); đọc nguyên văn lên nghe như đọc đề thi. Ngay sau khi khoá câu hỏi
cho một buổi, module này nhờ AI viết lại mỗi câu kỹ thuật thành câu người phỏng
vấn sẽ hỏi miệng, rồi lưu vào ``question_snapshot.spokenText``.

Bảo đảm:

* Chỉ đổi cách nói. Chấm điểm, phân loại ý định và mọi quyết định vẫn dùng
  ``questionText`` gốc; ``spokenText`` chỉ dùng khi hỏi và khi nhắc lại.
* Mỗi bản nói phải qua ``validate_spoken_question``: giữ đủ thuật ngữ của câu
  gốc, không thêm thuật ngữ nào từ đáp án mong đợi (không lộ ý cần có), không
  chứa từ về điểm hay barem, đúng ngôn ngữ, độ dài gần câu gốc. Trượt thì giữ
  câu gốc.
* Không chặn việc khoá câu hỏi: lỗi, hết thời gian hay tắt bằng biến môi trường
  ``INTERVIEW_SPOKEN_QUESTIONS=0`` đều chỉ có nghĩa là buổi đó dùng câu gốc.
* Chạy sau khi khoá đã commit, nên không giữ khoá dòng trong lúc gọi AI, và
  không ảnh hưởng ``fingerprint`` (tính trên mã câu hỏi, không trên nội dung).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.trace_logging import trace_event

logger = logging.getLogger(__name__)

SPOKEN_REWRITE_TIMEOUT_SECONDS = 20.0
MAX_SPOKEN_CHARS = 400

# Từ tiếng Việt viết không dấu, không phải thuật ngữ: không bắt buộc giữ lại.
_ASCII_VI_WORDS = {
    "cho", "con", "khi", "hay", "theo", "trong", "sang", "nhanh", "nhau", "thay", "ban", "minh",
    "nao", "cac", "mot", "hai", "thi", "vao", "ra", "lam", "nhu", "tai", "sao", "gian", "ghi", "xem", "nam",
    "so", "la", "co", "de", "do", "di", "em", "anh", "ma", "ve", "ta", "ai", "gi", "ha", "ban", "chi", "nhe",
    "and", "or", "the", "a", "an", "of", "to", "in", "for", "with", "how", "what", "why", "you",
    "your", "is", "are", "do", "does", "can", "would", "when", "which", "that", "this",
}
_WORD_RE = re.compile(r"[\w.+#\-]+")
_ASCII_TERM_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+#.\-]*$")


def _tech_tokens(text_value: str) -> set[str]:
    """Whole words written only in Latin letters: technical terms (Redis, cache, TTL)."""
    tokens: set[str] = set()
    for word in _WORD_RE.findall(str(text_value or "")):
        if not _ASCII_TERM_RE.match(word.strip(".-")):
            continue  # a Vietnamese word (has diacritics), a number, or mixed
        cleaned = word.strip(".-").casefold()
        if len(cleaned) >= 2 and cleaned not in _ASCII_VI_WORDS:
            tokens.add(cleaned)
    return tokens


_BENIGN_SCORE_WORDS_RE = re.compile(r"\b(ưu nhược điểm|ưu điểm|nhược điểm|điểm mạnh|điểm yếu|điểm khác)\b", re.IGNORECASE)
_IMPERATIVE_OPENING_RE = re.compile(
    r"^\W*(?:hãy\s+)?(?:trình bày|giải thích|nêu|mô tả|phân tích|liệt kê|so sánh)\b", re.IGNORECASE
)
_TEMPLATE_WRAP_RE = re.compile(r"\bgiúp mình\b.*\bnhé\W*$", re.IGNORECASE)


def _similarity(a: str, b: str) -> float:
    from difflib import SequenceMatcher

    return SequenceMatcher(None, a.casefold(), b.casefold()).ratio()


def _expected_text(snapshot: dict[str, Any]) -> str:
    """Đáp án mong đợi của câu hỏi, gộp thành chữ để kiểm lộ ý."""
    parts: list[str] = []
    points = snapshot.get("expectedPoints")
    if isinstance(points, list):
        for point in points:
            parts.append(json.dumps(point, ensure_ascii=False) if not isinstance(point, str) else point)
    elif points:
        parts.append(str(points))
    rubric = snapshot.get("rubric")
    if rubric:
        parts.append(json.dumps(rubric, ensure_ascii=False) if not isinstance(rubric, str) else rubric)
    return " ".join(parts)


def validate_spoken_question(
    original: str,
    spoken: str,
    *,
    expected_text: str = "",
    is_vi: bool = True,
) -> bool:
    """True khi ``spoken`` là bản nói an toàn của ``original``."""
    # Imported here: the core package imports planning modules at load time.
    from src.modules.interviews.core.interview_engine import PROHIBITED_LEAK_PATTERNS
    from src.modules.interviews.core.interviewer_voice import is_evaluative
    from src.modules.interviews.core.language_policy import response_matches_locale

    original = str(original or "").strip()
    spoken = str(spoken or "").strip()
    if not original or not spoken or spoken == original:
        return False
    if "\n" in spoken or len(spoken) > MAX_SPOKEN_CHARS:
        return False
    if len(spoken) < 0.5 * len(original) or len(spoken) > max(1.6 * len(original), len(original) + 80):
        return False
    # "ưu nhược điểm", "điểm mạnh" are not about scores; without this the
    # score-leak pattern ("điểm") rejected ordinary questions.
    leak_text = _BENIGN_SCORE_WORDS_RE.sub(" ", spoken)
    if any(re.search(pattern, leak_text, re.IGNORECASE) for pattern in PROHIBITED_LEAK_PATTERNS):
        return False
    # A wrapped copy ("Bạn trình bày giúp mình … nhé") is longer but no more spoken.
    # The point is to turn a written instruction into a spoken question: a
    # version that still opens with "Trình bày…", wraps the original
    # ("… giúp mình … nhé") or is a near copy adds nothing. 0.95 because a
    # real rephrasing of a short question can stay close (0.90 for
    # "Các mức transaction isolation và hiện tượng tương ứng là gì?").
    if (
        _IMPERATIVE_OPENING_RE.match(spoken)
        or _TEMPLATE_WRAP_RE.search(spoken)
        or _similarity(original, spoken) > 0.95
    ):
        return False
    if is_evaluative(spoken) or not response_matches_locale(spoken, is_vi=is_vi):
        return False
    original_terms = _tech_tokens(original)
    spoken_terms = _tech_tokens(spoken)
    # Every technical term the question asks about must still be asked about.
    if not original_terms <= spoken_terms:
        return False
    # A term that appears only in the expected answer must not be introduced:
    # that would hint at the answer ("Redis cache, nhớ đặt TTL…").
    answer_only_terms = _tech_tokens(expected_text) - original_terms
    if spoken_terms & answer_only_terms:
        return False
    return True


def _rewrite_prompt(is_vi: bool) -> str:
    from src.modules.interviews.core.interviewer_voice import style_guide

    if is_vi:
        return (
            "Bạn chuyển câu hỏi phỏng vấn kỹ thuật từ văn viết sang câu NÓI, như người phỏng vấn hỏi trực tiếp.\n"
            "Quy tắc bắt buộc:\n"
            "- Giữ nguyên ý và phạm vi hỏi; không thêm gợi ý, ví dụ đáp án hay thuật ngữ mới; không bỏ ý nào.\n"
            "- Giữ nguyên mọi thuật ngữ tiếng Anh có trong câu gốc.\n"
            "- Đổi câu mệnh lệnh ('Trình bày…', 'Giải thích…', 'Nêu…', 'Mô tả…') thành CÂU HỎI TRỰC TIẾP như hỏi miệng.\n"
            "- KHÔNG dùng khuôn 'Bạn … giúp mình … nhé'; không thêm 'nhé' cuối câu hỏi; mỗi câu một cách mở đầu khác nhau.\n"
            "- Tối đa 2 câu ngắn, như nói miệng; độ dài gần câu gốc.\n"
            "- Không chào hỏi, không câu dẫn kiểu 'Câu hỏi tiếp theo'.\n"
            "Ví dụ (chỉ để hiểu cách viết, không chép):\n"
            "- 'Trình bày cách hoạt động của garbage collector trong Java.' → 'Garbage collector trong Java chạy như thế nào?'\n"
            "- 'Nêu ưu nhược điểm của GraphQL so với REST.' → 'Theo bạn, GraphQL hơn và kém REST ở chỗ nào?'\n"
            "- 'Mô tả cách bạn thiết kế một rate limiter.' → 'Nếu phải làm một rate limiter, bạn sẽ thiết kế ra sao?'\n\n"
            + style_guide(True)
            + '\n\nTrả về đúng 1 JSON: {"items": [{"id": "<id>", "spoken": "<câu nói>"}]}'
        )
    return (
        "Rewrite each technical interview question from written form into what an interviewer would SAY.\n"
        "Rules: keep the meaning and scope; add no hints, example answers or new terms; drop nothing; keep "
        "every technical term; at most two short sentences; no greeting or 'next question' lead-in.\n\n"
        + style_guide(False)
        + '\n\nReturn exactly one JSON: {"items": [{"id": "<id>", "spoken": "<spoken question>"}]}'
    )


def _parse_items(raw: Any) -> dict[str, str]:
    data = raw
    if isinstance(raw, str):
        cleaned = raw.strip()
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned)
        data = json.loads(cleaned)
    items = data.get("items") if isinstance(data, dict) else data
    out: dict[str, str] = {}
    for item in items or []:
        if isinstance(item, dict) and item.get("id") and item.get("spoken"):
            out[str(item["id"])] = str(item["spoken"]).strip()
    return out


GenerateFn = Callable[..., Awaitable[Any]]


async def add_spoken_versions(
    *,
    db: AsyncSession,
    session_id: str,
    locale: str,
    generate_fn: GenerateFn | None = None,
    timeout_seconds: float = SPOKEN_REWRITE_TIMEOUT_SECONDS,
) -> int:
    """Viết và lưu ``spokenText`` cho các câu kỹ thuật của buổi; trả về số câu đã lưu."""
    if os.getenv("INTERVIEW_SPOKEN_QUESTIONS", "1").strip().lower() in {"0", "false", "off", "no"}:
        return 0
    is_vi = str(locale or "vi").lower().startswith("vi")
    try:
        result = await db.execute(
            text(
                """
                SELECT id, question_snapshot FROM interview_turns
                WHERE session_id = :sid AND question_version_id IS NOT NULL
                ORDER BY turn_index
                """
            ),
            {"sid": session_id},
        )
        rows = [dict(row) for row in result.mappings().all()]
    except Exception as exc:  # noqa: BLE001 - optional enrichment
        logger.warning("spoken questions: cannot read turns: %s", exc)
        return 0

    pending: dict[str, dict[str, Any]] = {}
    for row in rows:
        snapshot = row.get("question_snapshot") or {}
        if isinstance(snapshot, str):
            try:
                snapshot = json.loads(snapshot)
            except ValueError:
                continue
        question = str(snapshot.get("questionText") or "").strip()
        if (
            not question
            or snapshot.get("spokenText")
            or str(snapshot.get("questionType") or "").lower() == "coding"
        ):
            continue
        pending[str(row["id"])] = snapshot
    if not pending:
        return 0

    if generate_fn is None:
        from src.modules.ai.facade import generate_text as generate_fn  # noqa: PLC0415

    payload = json.dumps(
        [{"id": turn_id, "question": snap["questionText"]} for turn_id, snap in pending.items()],
        ensure_ascii=False,
    )
    try:
        raw = await asyncio.wait_for(
            generate_fn(
                instructions=_rewrite_prompt(is_vi),
                input_text=payload,
                max_output_tokens=2000,
                temperature=0.3,
            ),
            timeout=timeout_seconds,
        )
        rewritten = _parse_items(raw)
    except Exception as exc:  # noqa: BLE001 - optional enrichment
        logger.warning("spoken questions: rewrite failed: %s", exc)
        trace_event("interviewer", "spoken_questions_failed", session_id=session_id, error_type=type(exc).__name__)
        return 0

    saved = 0
    rejected = 0
    rejected_turn_ids: list[str] = []
    for turn_id, snapshot in pending.items():
        spoken = rewritten.get(turn_id, "")
        if not validate_spoken_question(
            snapshot["questionText"], spoken, expected_text=_expected_text(snapshot), is_vi=is_vi
        ):
            rejected += int(bool(spoken))
            rejected_turn_ids.append(str(turn_id))
            continue
        await db.execute(
            text(
                """
                UPDATE interview_turns
                SET question_snapshot = question_snapshot || CAST(:extra AS jsonb), updated_at = now()
                WHERE id = :id AND session_id = :sid
                """
            ),
            {"id": turn_id, "sid": session_id, "extra": json.dumps({"spokenText": spoken}, ensure_ascii=False)},
        )
        saved += 1
    if saved:
        await db.commit()
    trace_event(
        "interviewer",
        "spoken_questions_saved",
        session_id=session_id,
        saved=saved,
        rejected=rejected,
        total=len(pending),
        # Turns kept on their written text (rewrite missing or rejected).
        rejected_turn_ids=rejected_turn_ids,
    )
    return saved
