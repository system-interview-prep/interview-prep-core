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
COMMIT_SILENCE_SECONDS = 1.8
SHORT_COMMIT_SILENCE_SECONDS = 2.2
SHORT_ANSWER_WORDS = 8
# Speak a short filler only when Interview Core takes longer than this.
PROCESSING_FILLER_SECONDS = 1.2
# A brief pause inside an answer is common; acknowledge it before the answer
# window commits the current transcript fragment.
ANSWER_PAUSE_NUDGE_SECONDS = 1.25
# Gently re-prompt once per question after this much silence.
REPROMPT_AFTER_SECONDS = 30.0

# --------------------------------------------------------------------------- #
# Adaptive endpointing (V2 spec §8, correction brief §4)
# --------------------------------------------------------------------------- #
# The fixed 1.8 / 2.2 s windows above wait the same amount after "Em xong rồi
# ạ." as after "Ừm, thì… em nghĩ là". Adaptive endpointing reads what was
# actually said and waits accordingly.
#
# The bands are the spec's: a finished-sounding answer 250-500 ms, an unclear
# one 500-1000 ms, a hesitating one 1000-2200 ms. Note the third band ENDS at
# today's long wait — a candidate who is still thinking is never rushed. The
# brief is explicit that thresholds must not be reduced blindly, so the gain
# comes from committing early only when the transcript says it is safe.
#
# These are starting points. §8 requires calibration against Qwen's own
# finalization delay and the observed false-cut rate before they are trusted,
# which is why the whole thing is off by default.
COMPLETE_COMMIT_SECONDS = 0.45
UNSURE_COMMIT_SECONDS = 0.9
HESITANT_COMMIT_SECONDS = 2.0

#: Low confidence makes a "complete"-looking transcript unsure instead.
LOW_CONFIDENCE = 0.55

_TAIL_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)


def _tail_word(text: str) -> str:
    """The last word, lowercased, with its diacritics intact.

    Deliberately not ``_tokens``: that folds diacritics away for word counting,
    and the cues here do not survive folding. "đó" (sentence-final "there")
    and "do" (the conjunction "because") both become "do", so a folded
    comparison would read a closing particle as a clause continuation and wait
    the long band on a finished answer.
    """
    words = _TAIL_WORD_RE.findall(str(text or "").lower())
    return words[-1] if words else ""


# Trailing words that mean the sentence is not finished. Vietnamese speech
# runs on these: "em dùng Redis và…" is a clause waiting for its other half.
_CONTINUATION_TAIL = frozenset({
    "và", "với", "hoặc", "nhưng", "mà", "nên", "để", "vì", "do", "nếu", "khi",
    "thì", "rằng", "là", "của", "cho", "trong", "theo", "sau", "trước", "bằng",
    "cách", "các", "một", "những",
    "and", "or", "but", "so", "because", "if", "when", "that", "with", "for", "to",
})
# Fillers that mean the candidate is still assembling the thought.
_HESITATION_TAIL = frozenset({
    "ừm", "ừ", "à", "ờ", "ơ", "hmm", "um", "uhm", "er", "eh",
    "kiểu", "tức", "nói", "chung", "cái", "thế", "ý",
})
# Particles that close a Vietnamese sentence politely; a transcript may carry
# no punctuation at all, so these often are the only completeness signal.
_FINAL_PARTICLE = frozenset({"ạ", "nhé", "nhá", "nhỉ", "thôi", "đấy", "đó", "rồi", "xong"})

ENDPOINT_COMPLETE = "complete"
ENDPOINT_UNSURE = "unsure"
ENDPOINT_HESITANT = "hesitant"


def adaptive_endpointing_enabled() -> bool:
    """``VOICE_ADAPTIVE_ENDPOINTING=1`` turns on the transcript-aware wait.

    Off by default: §8 wants these thresholds calibrated against Qwen's
    finalization and a measured false-cut rate first, and a wrong threshold
    cuts a candidate off mid-sentence.
    """
    return os.getenv("VOICE_ADAPTIVE_ENDPOINTING", "0").strip().lower() in {"1", "true", "yes", "on"}


def completeness(text: str, *, confidence: float | None = None, stable: bool = True) -> str:
    """Does this transcript sound like a finished thought?

    Reads four things, in the order they override each other:

    * **stability** — the transcript is still being revised, so whatever it
      ends with now may not be the end. Always hesitant.
    * **a hesitation tail** ("ừm", "kiểu", "tức là") — still assembling.
    * **a continuation tail** ("và", "nhưng", "để") — a clause waiting for the
      rest of itself.
    * **a completeness mark** — terminal punctuation or a closing particle.

    Confidence lowers a verdict but never raises one: a misheard transcript
    that happens to end in a full stop is not evidence the candidate stopped.
    """
    value = str(text or "").strip()
    if not value:
        return ENDPOINT_HESITANT
    if not stable:
        return ENDPOINT_HESITANT

    tail = _tail_word(value)
    if not tail:
        return ENDPOINT_HESITANT

    if tail in _HESITATION_TAIL:
        return ENDPOINT_HESITANT
    if tail in _CONTINUATION_TAIL:
        return ENDPOINT_HESITANT

    ends_sentence = value.rstrip().endswith((".", "!", "?", "…"))
    closes_politely = tail in _FINAL_PARTICLE
    if ends_sentence or closes_politely:
        if confidence is not None and confidence < LOW_CONFIDENCE:
            return ENDPOINT_UNSURE
        return ENDPOINT_COMPLETE
    return ENDPOINT_UNSURE


def adaptive_commit_delay(
    text: str, *, confidence: float | None = None, stable: bool = True
) -> tuple[float, str]:
    """The silence to wait for this transcript, and why.

    Returns the seconds and the verdict, so a caller can trace which band a
    turn landed in and a false cut can be traced back to the text that caused it.
    """
    verdict = completeness(text, confidence=confidence, stable=stable)
    seconds = {
        ENDPOINT_COMPLETE: _env_seconds(
            "VOICE_COMMIT_COMPLETE", COMPLETE_COMMIT_SECONDS, minimum=0.2, maximum=3.0
        ),
        ENDPOINT_UNSURE: _env_seconds(
            "VOICE_COMMIT_UNSURE", UNSURE_COMMIT_SECONDS, minimum=0.3, maximum=4.0
        ),
        ENDPOINT_HESITANT: _env_seconds(
            "VOICE_COMMIT_HESITANT", HESITANT_COMMIT_SECONDS, minimum=0.5, maximum=8.0
        ),
    }[verdict]
    return seconds, verdict

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
    #: Has the interim transcript stopped changing? Fed by ``note_interim``.
    transcript_stable: bool = False
    #: Which adaptive band released the last commit, for tracing false cuts.
    last_endpoint_verdict: str | None = None
    _last_interim: str = ""

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

    def note_interim(self, text: str) -> None:
        """Record an interim transcript, so the window knows if the tail moved.

        Transcript stability is one of the four signals §8 asks for. An interim
        that still differs from the last one means Qwen is revising the end of
        the utterance, and whatever it ends with now may not be the end.
        """
        value = str(text or "").strip()
        self.transcript_stable = bool(value) and value == self._last_interim
        self._last_interim = value

    def commit_delay(self) -> float:
        """Seconds of silence to wait before submitting what is buffered.

        Fixed windows unless ``VOICE_ADAPTIVE_ENDPOINTING`` is on, in which
        case the wait follows what the transcript actually sounds like
        (:func:`adaptive_commit_delay`). The verdict is kept on
        ``last_endpoint_verdict`` so a committed turn can be traced to the band
        that released it — which is how a false cut gets diagnosed.
        """
        if not adaptive_endpointing_enabled():
            self.last_endpoint_verdict = None
            return self.commit_silence if self.words >= SHORT_ANSWER_WORDS else self.short_commit_silence

        seconds, verdict = adaptive_commit_delay(
            self.text,
            confidence=min(self.confidences) if self.confidences else None,
            stable=self.transcript_stable,
        )
        self.last_endpoint_verdict = verdict
        # Never wait less than the fixed windows would for a very short buffer
        # that is not clearly finished: a lone mis-heard word must not end an
        # answer early just because it carries no continuation cue.
        if verdict != ENDPOINT_COMPLETE and self.words < SHORT_ANSWER_WORDS:
            seconds = max(seconds, min(self.short_commit_silence, HESITANT_COMMIT_SECONDS))
        return seconds

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


def _load_script(module_name: str, path: Path) -> ModuleType:
    """Load a data-only phrase module without importing Core in LiveKit worker."""

    cached = sys.modules.get(module_name)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:  # pragma: no cover - the file ships with the repo
        raise ImportError(f"interviewer script not found at {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


VOICE_SCRIPT = _load_script("interview_voice_phrases", Path(__file__).resolve().parents[0] / "voice_phrases.py")

ANSWER_PAUSE_NUDGES_VI: tuple[str, ...] = VOICE_SCRIPT.ANSWER_PAUSE_NUDGES_VI
ANSWER_PAUSE_NUDGES_EN: tuple[str, ...] = VOICE_SCRIPT.ANSWER_PAUSE_NUDGES_EN
PROCESSING_FILLERS_VI: tuple[str, ...] = VOICE_SCRIPT.PROCESSING_FILLERS_VI
PROCESSING_FILLERS_EN: tuple[str, ...] = VOICE_SCRIPT.PROCESSING_FILLERS_EN
# Voice-only recovery phrases stay outside the shared Core dialogue policy.
NOT_HEARD_VI: tuple[str, ...] = VOICE_SCRIPT.NOT_HEARD_VI
NOT_HEARD_EN: tuple[str, ...] = VOICE_SCRIPT.NOT_HEARD_EN


def _is_vi(locale: str) -> bool:
    return str(locale or "").lower().startswith("vi")


def not_heard_lines(locale: str) -> tuple[str, ...]:
    return NOT_HEARD_VI if _is_vi(locale) else NOT_HEARD_EN


def processing_fillers(locale: str) -> tuple[str, ...]:
    return PROCESSING_FILLERS_VI if _is_vi(locale) else PROCESSING_FILLERS_EN


def processing_filler_enabled() -> bool:
    """Opt in to the local processing line; default to origin's silent behavior."""
    return os.getenv("VOICE_PROCESSING_FILLER_ENABLED", "false").strip().lower() in {
        "1", "true", "yes", "on"
    }


def pick_processing_filler(locale: str, seed: str, avoid: str | None = None) -> str:
    """Choose a stable filler for this turn, avoiding the previous line when possible."""
    import hashlib

    pool = processing_fillers(locale)
    start = int(hashlib.sha256(seed.encode("utf-8")).hexdigest(), 16) % len(pool)
    for offset in range(len(pool)):
        line = pool[(start + offset) % len(pool)]
        if line != avoid:
            return line
    return pool[start]


def pick_answer_pause_nudge(locale: str, seed: str, avoid: str | None = None) -> str:
    """Vary the pause backchannel by turn and avoid repeating the last line."""
    import hashlib

    pool = ANSWER_PAUSE_NUDGES_VI if _is_vi(locale) else ANSWER_PAUSE_NUDGES_EN
    start = int(hashlib.sha256(seed.encode("utf-8")).hexdigest(), 16) % len(pool)
    for offset in range(len(pool)):
        line = pool[(start + offset) % len(pool)]
        if line != avoid:
            return line
    return pool[start]


def voice_dialect() -> str:
    """Voice-only spoken dialect (INTERVIEWER_VOICE_DIALECT=bac|nam)."""
    value = os.getenv("INTERVIEWER_VOICE_DIALECT", "bac").strip().casefold()
    return "nam" if value in {"nam", "south", "southern", "mien nam", "miền nam"} else "bac"


def localize_particles(text: str) -> str:
    """Southern voice: "nhé" → "nha", "nhỉ" → "hen" for the lines only the room says."""
    value = str(text or "")
    if voice_dialect() != "nam":
        return value
    value = re.sub(r"(?<!\w)nhé(?!\w)", "nha", value)
    return re.sub(r"(?<!\w)nhỉ(?!\w)", "hen", value)


def silence_reprompt(locale: str) -> str:
    return VOICE_SCRIPT.SILENCE_REPROMPT_VI if _is_vi(locale) else VOICE_SCRIPT.SILENCE_REPROMPT_EN


def turn_taking_tip(locale: str) -> str:
    """Briefly invite a natural answer before the opening question."""
    return VOICE_SCRIPT.TURN_TAKING_TIP_VI if _is_vi(locale) else VOICE_SCRIPT.TURN_TAKING_TIP_EN


def unclear_transcript_text(locale: str) -> str:
    return VOICE_SCRIPT.UNCLEAR_TRANSCRIPT_VI if _is_vi(locale) else VOICE_SCRIPT.UNCLEAR_TRANSCRIPT_EN


def core_error_text(locale: str) -> str:
    return VOICE_SCRIPT.CORE_ERROR_VI if _is_vi(locale) else VOICE_SCRIPT.CORE_ERROR_EN


def backchannel_continue_text(locale: str) -> str:
    return VOICE_SCRIPT.BACKCHANNEL_CONTINUE_VI if _is_vi(locale) else VOICE_SCRIPT.BACKCHANNEL_CONTINUE_EN


def reprompt_after_seconds() -> float:
    return _env_seconds("VOICE_REPROMPT_AFTER_SECONDS", REPROMPT_AFTER_SECONDS, minimum=10.0, maximum=120.0)
