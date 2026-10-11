"""Answer window: turn many STT fragments into one interview answer.

Speech recognition ends a "turn" at every short pause, and the interview used
to treat each fragment ("Yes, that TypeScript.", "That.") as a whole answer,
so one spoken answer advanced the interview several questions. Interview
answers are long and full of thinking pauses, so the voice agent buffers the
fragments of one answer here and submits them to Interview Core only after a
longer silence, or when the candidate says they are done (or presses "Xong").

Pure and synchronous so it can be unit tested without LiveKit.
"""

from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass, field

# Silence (seconds) after the last fragment before an answer is submitted.
# A short buffer waits longer, so a brief real answer ("Chưa ạ.") still goes
# through while a lone mis-heard word does not end the answer prematurely.
COMMIT_SILENCE_SECONDS = 2.5
SHORT_COMMIT_SILENCE_SECONDS = 5.0
SHORT_ANSWER_WORDS = 8
# Speak a short filler only when Interview Core takes longer than this.
PROCESSING_FILLER_SECONDS = 2.5
# Gently re-prompt once per question after this much silence.
REPROMPT_AFTER_SECONDS = 30.0

_VI_DONE_PHRASES = (
    "xong",
    "xong roi",
    "em xong roi",
    "em tra loi xong",
    "em tra loi xong roi",
    "toi tra loi xong",
    "minh tra loi xong",
    "het roi",
    "het a",
    "em het roi",
    "vay thoi",
    "chi vay thoi",
)
# Apostrophes split tokens: "that's all" becomes "that s all".
_EN_DONE_PHRASES = (
    "done",
    "i am done",
    "i m done",
    "that is all",
    "that s all",
    "that is it",
    "that s it",
)
# "xong" inside a sentence ("deploy xong thì...") is content, not a command:
# only a short fragment that is, or ends with, a done phrase counts.
_MAX_DONE_FRAGMENT_WORDS = 6


def _env_seconds(name: str, default: float, *, minimum: float, maximum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError:
        return default
    return min(maximum, max(minimum, value))


def _tokens(text: str) -> list[str]:
    folded = unicodedata.normalize("NFKD", text.lower()).replace("đ", "d")
    ascii_text = "".join(char for char in folded if not unicodedata.combining(char))
    return re.findall(r"[a-z0-9]+", ascii_text)


def is_done_command(text: str, *, locale: str = "vi-VN") -> bool:
    """True for a short fragment that hands the floor back ("em trả lời xong ạ")."""
    tokens = [token for token in _tokens(text) if token not in {"a", "nhe", "nha"}]
    if not tokens or len(tokens) > _MAX_DONE_FRAGMENT_WORDS:
        return False
    phrase = " ".join(tokens)
    phrases = _VI_DONE_PHRASES if locale.lower().startswith("vi") else _EN_DONE_PHRASES
    return any(phrase == done or phrase.endswith(f" {done}") for done in phrases)


def word_count(text: str) -> int:
    return len(_tokens(text))


@dataclass
class AnswerWindow:
    """The fragments of the answer currently being spoken."""

    commit_silence: float = field(
        default_factory=lambda: _env_seconds(
            "VOICE_ANSWER_COMMIT_SILENCE", COMMIT_SILENCE_SECONDS, minimum=0.8, maximum=8.0
        )
    )
    short_commit_silence: float = field(
        default_factory=lambda: _env_seconds(
            "VOICE_ANSWER_SHORT_COMMIT_SILENCE", SHORT_COMMIT_SILENCE_SECONDS, minimum=1.0, maximum=12.0
        )
    )
    fragments: list[str] = field(default_factory=list)
    raw_fragments: list[str] = field(default_factory=list)
    confidences: list[float] = field(default_factory=list)
    first_message_id: str | None = None
    duration_seconds: float = 0.0

    @property
    def is_empty(self) -> bool:
        return not self.fragments

    @property
    def text(self) -> str:
        return " ".join(fragment.strip() for fragment in self.fragments if fragment.strip())

    @property
    def words(self) -> int:
        return word_count(self.text)

    def add(
        self,
        text: str,
        *,
        message_id: str,
        raw_text: str | None = None,
        duration: float = 0.0,
        confidence: float | None = None,
    ) -> None:
        if not text.strip():
            return
        if self.first_message_id is None:
            self.first_message_id = message_id
        self.fragments.append(text.strip())
        self.raw_fragments.append((raw_text or text).strip())
        if isinstance(confidence, (int, float)):
            self.confidences.append(min(1.0, max(0.0, float(confidence))))
        self.duration_seconds += max(0.0, duration)

    def commit_delay(self) -> float:
        """Seconds of silence to wait before submitting what is buffered."""
        return self.commit_silence if self.words >= SHORT_ANSWER_WORDS else self.short_commit_silence

    def take(self) -> SubmittedAnswer | None:
        """Return the buffered answer and start a new one."""
        if self.is_empty:
            return None
        answer = SubmittedAnswer(
            text=self.text,
            raw_text=" ".join(self.raw_fragments),
            # One id per answer keeps retries idempotent in Interview Core.
            client_message_id=f"livekit:{self.first_message_id}",
            fragment_count=len(self.fragments),
            duration_seconds=self.duration_seconds,
            confidence=min(self.confidences) if self.confidences else None,
        )
        self.fragments.clear()
        self.raw_fragments.clear()
        self.confidences.clear()
        self.first_message_id = None
        self.duration_seconds = 0.0
        return answer


@dataclass(frozen=True)
class SubmittedAnswer:
    text: str
    raw_text: str
    client_message_id: str
    fragment_count: int
    duration_seconds: float
    confidence: float | None


def processing_filler(locale: str) -> str:
    return "Mình đang xem câu trả lời, chờ mình một chút nhé." if locale.lower().startswith("vi") else (
        "Give me a moment to go through your answer."
    )


def silence_reprompt(locale: str) -> str:
    if locale.lower().startswith("vi"):
        return (
            "Bạn cứ từ từ suy nghĩ nhé. Muốn nghe lại câu hỏi thì nói “nhắc lại”; "
            "trả lời xong thì nói “xong” hoặc bấm nút Xong."
        )
    return "Take your time. Say “repeat” to hear the question again, or “done” when you have finished."


def reprompt_after_seconds() -> float:
    return _env_seconds("VOICE_REPROMPT_AFTER_SECONDS", REPROMPT_AFTER_SECONDS, minimum=10.0, maximum=120.0)
