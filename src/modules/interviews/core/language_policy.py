"""Deterministic output-language checks for generated interviewer speech."""

from __future__ import annotations

import re

_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)
_VI_WORDS = {
    "bạn", "có", "thể", "hãy", "vì", "sao", "như", "thế", "nào", "và",
    "đã", "một", "trong", "khi", "của", "được", "không", "gì", "mình",
    "câu", "hỏi", "trả", "lời", "chia", "sẻ", "cảm", "ơn", "hay", "nếu",
}
_EN_WORDS = {
    "you", "your", "why", "what", "how", "when", "where", "which", "and",
    "the", "is", "are", "do", "did", "would", "could", "please", "tell",
    "explain", "describe", "question", "answer", "thanks", "thank", "can",
    "about", "approach", "handle", "give", "example", "difference", "between",
}
_ENGLISH_OPENERS = re.compile(
    r"^(?:how|what|why|when|where|which|can|could|would|please|tell|explain|describe)\b",
    re.IGNORECASE,
)


def response_matches_locale(text: str, *, is_vi: bool) -> bool:
    """Reject clearly wrong-language prose while allowing technical code-switching."""

    words = {word.casefold() for word in _WORD.findall(text)}
    vi_score = len(words & _VI_WORDS)
    en_score = len(words & _EN_WORDS)
    if is_vi:
        clearly_english = en_score >= 2 or bool(_ENGLISH_OPENERS.match(text.strip()))
        return not (clearly_english and vi_score == 0)
    return not (vi_score >= 2 and en_score == 0)
