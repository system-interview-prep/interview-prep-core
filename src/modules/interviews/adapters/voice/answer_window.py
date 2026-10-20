"""Answer window: turn many STT fragments into one interview answer.

Speech recognition ends a "turn" at every short pause, and the interview used
to treat each fragment ("Yes, that TypeScript.", "That.") as a whole answer,
so one spoken answer advanced the interview several questions. Interview
answers are long and full of thinking pauses, so the voice agent buffers the
fragments of one answer here and submits them to Interview Core after an
adaptive silence following the candidate's last fragment.

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
SHORT_COMMIT_SILENCE_SECONDS = 2.5
SHORT_ANSWER_WORDS = 8
# Speak a short filler only when Interview Core takes longer than this.
PROCESSING_FILLER_SECONDS = 1.2
# Gently re-prompt once per question after this much silence.
REPROMPT_AFTER_SECONDS = 30.0

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


# Said while Interview Core works on an answer. Rotated per answer so the same
# sentence is not heard after every question.
PROCESSING_FILLERS_VI: tuple[str, ...] = (
    "Mình đã ghi nhận. Chờ mình một chút nhé.",
    "Ừm, để mình xem lại ý bạn vừa nói nhé.",
    "Ok, bạn chờ mình vài giây nhé.",
    "Mình đang xem câu trả lời của bạn, chờ chút nhé.",
)
PROCESSING_FILLERS_EN: tuple[str, ...] = (
    "Got it. Give me a moment.",
    "Okay, let me think about that for a second.",
    "Right, one moment please.",
)


# Khi không nghe được câu trả lời (bản ghi rỗng hoặc quá nhiễu).
NOT_HEARD_VI: tuple[str, ...] = (
    "Mình chưa nghe rõ câu trả lời. Bạn vui lòng nói lại nhé.",
    "Hình như mình nghe chưa rõ, bạn nói lại giúp mình nhé.",
    "Tiếng hơi nhỏ, bạn nói lại câu trả lời được không?",
)
NOT_HEARD_EN: tuple[str, ...] = (
    "I didn't catch that. Could you say it again?",
    "Sorry, I missed that. Could you repeat it?",
)


def not_heard_lines(locale: str) -> tuple[str, ...]:
    return NOT_HEARD_VI if locale.lower().startswith("vi") else NOT_HEARD_EN


def processing_fillers(locale: str) -> tuple[str, ...]:
    return PROCESSING_FILLERS_VI if locale.lower().startswith("vi") else PROCESSING_FILLERS_EN


def processing_filler(locale: str, index: int = 0) -> str:
    pool = processing_fillers(locale)
    return pool[index % len(pool)]


def silence_reprompt(locale: str) -> str:
    if locale.lower().startswith("vi"):
        return (
            "Bạn cứ từ từ suy nghĩ nhé. Khi sẵn sàng, hãy trả lời tự nhiên; "
            "nếu muốn nghe lại, chỉ cần nói “nhắc lại câu hỏi”."
        )
    return (
        "Take your time and answer naturally when you're ready. "
        "If you want to hear it again, just say “repeat the question”."
    )


def turn_taking_tip(locale: str) -> str:
    """Briefly invite a natural answer before the opening question."""
    if locale.lower().startswith("vi"):
        return (
            "Bạn cứ trả lời tự nhiên. Nếu cần nghe lại, hãy nói “nhắc lại câu hỏi”."
        )
    return (
        "Answer naturally. If you need to hear a question again, just say “repeat the question”."
    )


def reprompt_after_seconds() -> float:
    return _env_seconds("VOICE_REPROMPT_AFTER_SECONDS", REPROMPT_AFTER_SECONDS, minimum=10.0, maximum=120.0)
