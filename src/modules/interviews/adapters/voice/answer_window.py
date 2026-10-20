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

import importlib.util
import os
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType

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


def _load_script() -> ModuleType:
    """The shared interviewer script (core/interviewer_script.py).

    The LiveKit worker runs this folder as plain scripts, without the ``src``
    package, so the data-only script file is loaded by its path. Loading it
    the same way inside the API avoids importing the whole core package (and
    its engine) from a transport adapter.
    """
    cached = sys.modules.get("interviewer_script")
    if cached is not None:
        return cached
    path = Path(__file__).resolve().parents[2] / "core" / "interviewer_script.py"
    spec = importlib.util.spec_from_file_location("interviewer_script", path)
    if spec is None or spec.loader is None:  # pragma: no cover - the file ships with the repo
        raise ImportError(f"interviewer script not found at {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["interviewer_script"] = module
    spec.loader.exec_module(module)
    return module


SCRIPT = _load_script()

# Câu chỉ có ở kênh nói. Câu chữ nằm ở core/interviewer_script.py.
PROCESSING_FILLERS_VI: tuple[str, ...] = SCRIPT.PROCESSING_FILLERS_VI
PROCESSING_FILLERS_EN: tuple[str, ...] = SCRIPT.PROCESSING_FILLERS_EN
NOT_HEARD_VI: tuple[str, ...] = SCRIPT.NOT_HEARD_VI
NOT_HEARD_EN: tuple[str, ...] = SCRIPT.NOT_HEARD_EN


def _is_vi(locale: str) -> bool:
    return str(locale or "").lower().startswith("vi")


def not_heard_lines(locale: str) -> tuple[str, ...]:
    return NOT_HEARD_VI if _is_vi(locale) else NOT_HEARD_EN


def processing_fillers(locale: str) -> tuple[str, ...]:
    return PROCESSING_FILLERS_VI if _is_vi(locale) else PROCESSING_FILLERS_EN


def processing_filler(locale: str, index: int = 0) -> str:
    pool = processing_fillers(locale)
    return pool[index % len(pool)]


def silence_reprompt(locale: str) -> str:
    return SCRIPT.SILENCE_REPROMPT_VI if _is_vi(locale) else SCRIPT.SILENCE_REPROMPT_EN


def turn_taking_tip(locale: str) -> str:
    """Briefly invite a natural answer before the opening question."""
    return SCRIPT.TURN_TAKING_TIP_VI if _is_vi(locale) else SCRIPT.TURN_TAKING_TIP_EN


def unclear_transcript_text(locale: str) -> str:
    return SCRIPT.UNCLEAR_TRANSCRIPT_VI if _is_vi(locale) else SCRIPT.UNCLEAR_TRANSCRIPT_EN


def core_error_text(locale: str) -> str:
    return SCRIPT.CORE_ERROR_VI if _is_vi(locale) else SCRIPT.CORE_ERROR_EN


def backchannel_continue_text(locale: str) -> str:
    return SCRIPT.BACKCHANNEL_CONTINUE_VI if _is_vi(locale) else SCRIPT.BACKCHANNEL_CONTINUE_EN


def reprompt_after_seconds() -> float:
    return _env_seconds("VOICE_REPROMPT_AFTER_SECONDS", REPROMPT_AFTER_SECONDS, minimum=10.0, maximum=120.0)
