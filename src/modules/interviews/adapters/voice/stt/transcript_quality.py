"""Conservative quality checks for finalized speech-to-text fragments."""

from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass


def _confidence_threshold() -> float:
    try:
        value = float(os.getenv("VOICE_LAB_STT_MIN_CONFIDENCE", "0.45"))
    except ValueError:
        return 0.45
    return min(0.95, max(0.0, value))


def _tokens(text: str) -> list[str]:
    return re.findall(r"[\w]+", text.casefold(), flags=re.UNICODE)


_VI_MARKERS = {
    "anh", "ban", "bạn", "cho", "co", "có", "cua", "của", "da", "đã",
    "dang", "đang", "duoc", "được", "em", "khong", "không", "la", "là",
    "minh", "mình", "mot", "một", "nay", "này", "nhung", "nhưng", "toi", "tôi",
    "va", "và", "voi", "với",
}
_TECHNICAL_TERMS = {
    "api", "aws", "azure", "backend", "cache", "ci", "cd", "cloud", "database",
    "docker", "fastapi", "frontend", "git", "github", "java", "javascript", "jwt",
    "kafka", "kubernetes", "linux", "microservice", "microservices", "mongodb", "mysql",
    "node", "nodejs", "postgresql", "python", "react", "redis", "rest", "sql", "typescript",
}
_ENGLISH_FUNCTION_WORDS = {
    "a", "am", "and", "are", "as", "at", "be", "for", "from", "i", "im", "in", "is",
    "it", "my", "of", "on", "that", "the", "this", "to", "we", "with", "you", "your",
}
_COMMON_ENGLISH_NOISE = {
    "bye", "hello", "hello everyone", "okay", "please subscribe", "thank you",
    "thank you for watching", "thanks for watching", "the end", "you",
}


def _has_vietnamese_diacritic(text: str) -> bool:
    decomposed = unicodedata.normalize("NFD", text.casefold())
    return "đ" in text.casefold() or any(unicodedata.combining(char) for char in decomposed)


def _looks_like_english_noise(text: str, tokens: list[str]) -> bool:
    phrase = " ".join(tokens)
    if phrase in _COMMON_ENGLISH_NOISE:
        return True
    if not tokens or any(token.isdigit() for token in tokens):
        return False
    token_set = set(tokens)
    if token_set & _TECHNICAL_TERMS:
        return False
    if token_set & _VI_MARKERS or _has_vietnamese_diacritic(text):
        return False
    english_words = sum(token in _ENGLISH_FUNCTION_WORDS for token in tokens)
    return len(tokens) >= 3 and english_words / len(tokens) >= 0.4


@dataclass(frozen=True)
class TranscriptQuality:
    accepted: bool
    reason: str
    confidence: float | None
    word_count: int


def assess_transcript(
    text: str,
    *,
    confidence: float | None,
    min_confidence: float | None = None,
    locale: str | None = None,
) -> TranscriptQuality:
    """Reject high-signal STT failures without penalizing technical code-switching."""
    tokens = _tokens(text)
    threshold = _confidence_threshold() if min_confidence is None else min_confidence
    normalized_confidence = (
        min(1.0, max(0.0, float(confidence)))
        if isinstance(confidence, (int, float))
        else None
    )

    if not tokens:
        return TranscriptQuality(False, "empty", normalized_confidence, 0)
    if normalized_confidence is not None and normalized_confidence < threshold:
        return TranscriptQuality(False, "low_confidence", normalized_confidence, len(tokens))
    if locale and locale.lower().startswith("vi") and _looks_like_english_noise(text, tokens):
        return TranscriptQuality(False, "locale_mismatch", normalized_confidence, len(tokens))

    if len(tokens) >= 4:
        most_common = max(tokens.count(token) for token in set(tokens))
        if most_common / len(tokens) >= 0.75:
            return TranscriptQuality(False, "repeated_token", normalized_confidence, len(tokens))

    return TranscriptQuality(True, "accepted", normalized_confidence, len(tokens))
