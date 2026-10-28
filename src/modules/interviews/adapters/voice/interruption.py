"""Pure helpers for natural voice interruption handling.

Keeping this module independent from LiveKit makes the conversation policy
easy to test without installing the optional realtime dependencies.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass
from typing import Literal

try:
    from .answer_window import SCRIPT
except ImportError:  # Executed directly by the API's LiveKit subprocess.
    from answer_window import SCRIPT  # type: ignore[no-redef]

InterruptionIntent = Literal["backchannel", "hold", "content"]
InterruptionAction = Literal["resume", "hold", "forward"]

_VI_GREETINGS = {"alo", "hello", "xin chao"}
_EN_GREETINGS = {"hello", "hi", "hey"}
# Repeat requests are matched on the text with its accents: folded, "họ"
# (they) would read as "hộ" (please) and "chỉ" as "chị".
_VI_NOT_HEARD = re.compile(
    r"\b(chưa|không|ko)\s+nghe\s+(rõ|được|thấy|kịp)\b"
    r"|\bnghe\s+(chưa|không|ko)\s+(rõ|được|kịp)\b"
    r"|\b(hỏi|nói)\s+gì\s+cơ\b|^\W*(dạ\W+)?gì\s+cơ\b"
    r"|\bcâu\s+hỏi\s+là\s+gì\b|\bnói\s+chậm\s+lại\b"
)
# "Bị ngắt" is also an answer ("kết nối DB bị ngắt thì em retry"): it is an
# audio complaint only as a very short aside.
_VI_AUDIO_PROBLEM = re.compile(r"\b(đứt\s+đoạn|gián\s+đoạn|bị\s+ngắt|bị\s+rè|mất\s+tiếng)\b")
_MAX_AUDIO_PROBLEM_WORDS = 6
# "Nói lại", "hỏi lại"... are also answer words ("em hỏi lại PM"), so they ask
# for a repeat only when addressed to the interviewer.
_VI_REPEAT_VERB = re.compile(r"\b(nói|nhắc|lặp|hỏi|đọc|nghe)\s+lại\b")
_VI_REQUEST_CUE = re.compile(r"\b(giúp|giùm|hộ|nhé|nha|được\s+không|được\s+ko|câu\s+hỏi)\b|\bđi\W*$")
_VI_ADDRESSEE = re.compile(r"^\W*((dạ|vâng|ơ)\W+)?(bạn|anh|chị|cho\s+(em|mình|tôi)|xin|làm\s+ơn)\b")
_EN_REPEAT_MARKERS = (
    "audio cut out",
    "audio is cutting out",
    "could not hear",
    "couldn t hear",
    "did not hear",
    "didn t hear",
    "didn t catch",
    "did not catch",
    "repeat that",
    "repeat the question",
    "can you repeat",
    "could you repeat",
    "say again",
    "come again",
    "pardon",
    "what was the question",
)
# A repeat request is a short aside; a long utterance is answer content even
# when it mentions "lặp lại" (retry loops) or "hỏi lại" (asking a teammate).
_MAX_REPEAT_REQUEST_WORDS = 14
_VI_RESUME_MARKERS = {
    "duoc roi",
    "noi tiep di",
    "tiep tuc",
    "tiep tuc di",
    "toi san sang roi",
    "xong roi",
}
_EN_RESUME_MARKERS = {
    "continue",
    "go ahead",
    "i am ready",
    "ready",
    "you can continue",
}
_NON_LEXICAL_NOISE_MARKERS = {
    "background noise",
    "cough",
    "coughing",
    "laughter",
    "music",
    "noise",
    "silence",
    "static",
}


@dataclass(frozen=True)
class InterruptionSnapshot:
    occurred: bool = False
    speech_id: str | None = None
    expected_text: str = ""
    delivered_text: str = ""

    def telemetry(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class InterruptionResolution:
    """Transport decision for speech that overlaps interviewer playout."""

    action: InterruptionAction
    reason: str
    resume_text: str = ""

    @property
    def should_forward(self) -> bool:
        return self.action == "forward"


def _normalized_tokens(text: str) -> list[str]:
    folded = unicodedata.normalize("NFKD", text.lower())
    ascii_text = "".join(char for char in folded if not unicodedata.combining(char))
    ascii_text = ascii_text.replace("đ", "d")
    return re.findall(r"[a-z0-9]+", ascii_text)


def _is_non_lexical_noise(text: str) -> bool:
    phrase = " ".join(_normalized_tokens(text))
    return not phrase or phrase in _NON_LEXICAL_NOISE_MARKERS


def classify_interruption(text: str, *, locale: str = "vi-VN") -> InterruptionIntent:
    """Classify only short overlap utterances; substantive speech stays content."""
    tokens = _normalized_tokens(text)
    if not tokens or len(tokens) > 3:
        return "content"

    phrase = " ".join(tokens)
    if locale.lower().startswith("vi"):
        backchannels = {
            "a",
            "alo",
            "da",
            "duoc",
            "ok",
            "okay",
            "roi",
            "uh",
            "uhm",
            "um",
            "u",
            "um hum",
            "vang",
            "vay a",
            "hello",
            "xin chao",
        }
        holds = {"cho chut", "cho toi", "doi chut", "khoan", "khoan da", "tu tu"}
    else:
        backchannels = {
            "got it", "hello", "mhm", "ok", "okay", "right", "uh", "uh huh", "uhm", "yep", "yes"
        }
        holds = {"hold on", "one moment", "wait", "wait please"}

    if phrase in backchannels:
        return "backchannel"
    if phrase in holds:
        return "hold"
    return "content"


def is_greeting_backchannel(text: str, *, locale: str = "vi-VN") -> bool:
    """Return whether a short utterance is a greeting rather than an answer."""
    phrase = " ".join(_normalized_tokens(text))
    greetings = _VI_GREETINGS if locale.lower().startswith("vi") else _EN_GREETINGS
    return phrase in greetings


def is_repeat_request(text: str, *, locale: str = "vi-VN") -> bool:
    """Detect explicit reports that the question was missed or cut off."""
    tokens = _normalized_tokens(text)
    if not tokens or len(tokens) > _MAX_REPEAT_REQUEST_WORDS:
        return False
    if not locale.lower().startswith("vi"):
        # Pad so markers match whole words only.
        phrase = f" {' '.join(tokens)} "
        # The re-prompt tells candidates to just say "repeat".
        if len(tokens) <= 3 and "repeat" in tokens:
            return True
        return any(f" {marker} " in phrase for marker in _EN_REPEAT_MARKERS)
    spoken = unicodedata.normalize("NFC", text).lower().strip()
    if _VI_NOT_HEARD.search(spoken):
        return True
    if _VI_AUDIO_PROBLEM.search(spoken):
        return len(tokens) <= _MAX_AUDIO_PROBLEM_WORDS
    if not _VI_REPEAT_VERB.search(spoken):
        return False
    return len(tokens) <= 3 or bool(_VI_REQUEST_CUE.search(spoken) or _VI_ADDRESSEE.search(spoken))


def is_resume_request(text: str, *, locale: str = "vi-VN") -> bool:
    """Detect a candidate explicitly handing the floor back after a hold."""
    phrase = " ".join(_normalized_tokens(text))
    markers = _VI_RESUME_MARKERS if locale.lower().startswith("vi") else _EN_RESUME_MARKERS
    return phrase in markers


def is_probable_playout_echo(transcript: str, expected_text: str) -> bool:
    """Identify a non-trivial contiguous fragment echoed from current TTS.

    One- and two-word overlaps are intentionally accepted as candidate speech;
    common interview vocabulary such as "Redis" or "kinh nghiệm" must never be
    discarded just because it also appeared in the question.
    """
    heard = _normalized_tokens(transcript)
    expected = _normalized_tokens(expected_text)
    if len(heard) < 4 or len(expected) < len(heard):
        return False
    width = len(heard)
    return any(expected[index : index + width] == heard for index in range(len(expected) - width + 1))


def delivered_text_from_handle(handle: object | None) -> str:
    """Read LiveKit's playout-truncated assistant text without importing LiveKit."""
    if handle is None:
        return ""
    items = getattr(handle, "chat_items", None)
    if not isinstance(items, list):
        return ""
    for item in reversed(items):
        text = getattr(item, "text_content", None)
        if isinstance(text, str) and text.strip():
            return text.strip()
    return ""


def capture_interruption(handle: object | None, expected_text: str) -> InterruptionSnapshot:
    if handle is None or not bool(getattr(handle, "interrupted", False)):
        return InterruptionSnapshot()
    speech_id = getattr(handle, "id", None)
    return InterruptionSnapshot(
        occurred=True,
        speech_id=str(speech_id) if speech_id else None,
        expected_text=expected_text.strip(),
        delivered_text=delivered_text_from_handle(handle),
    )


def remaining_after_interruption(expected_text: str, delivered_text: str) -> str:
    """Return only the unheard suffix when LiveKit provides an exact prefix."""
    expected = expected_text.strip()
    delivered = delivered_text.strip()
    if not expected:
        return ""
    # LiveKit can flag an interruption before the transcript synchronizer has
    # committed even the first spoken word. In that case the candidate heard
    # no reliable prefix, so replay the complete question instead of silently
    # consuming the interview turn.
    if not delivered:
        return expected
    if expected.startswith(delivered):
        return expected[len(delivered) :].lstrip(" \n\t,.;:!?-")

    # Transcript synchronization can normalize whitespace while preserving the
    # same words. Map the delivered word count back onto the original string.
    expected_matches = list(re.finditer(r"\S+", expected))
    delivered_words = delivered.split()
    if delivered_words and len(delivered_words) < len(expected_matches):
        expected_prefix = " ".join(match.group(0) for match in expected_matches[: len(delivered_words)])
        if _normalized_tokens(expected_prefix) == _normalized_tokens(delivered):
            end = expected_matches[len(delivered_words) - 1].end()
            return expected[end:].lstrip(" \n\t,.;:!?-")
    return ""


def resolve_interruption(
    transcript: str,
    snapshot: InterruptionSnapshot,
    *,
    locale: str = "vi-VN",
) -> InterruptionResolution:
    """Resolve overlap without letting noise or echo reach Interview Core."""
    if not snapshot.occurred:
        return InterruptionResolution(action="forward", reason="normal_turn")

    def resumable_text() -> str:
        remaining = remaining_after_interruption(snapshot.expected_text, snapshot.delivered_text)
        # If SDK transcript synchronization diverged, replaying the coherent
        # utterance is safer than silently losing the active question.
        return remaining or snapshot.expected_text

    if _is_non_lexical_noise(transcript):
        return InterruptionResolution(
            action="resume",
            reason="empty_or_non_lexical_noise",
            resume_text=resumable_text(),
        )
    if is_probable_playout_echo(transcript, snapshot.expected_text):
        return InterruptionResolution(
            action="resume",
            reason="tts_playout_echo",
            resume_text=resumable_text(),
        )

    intent = classify_interruption(transcript, locale=locale)
    if intent == "backchannel":
        return InterruptionResolution(
            action="resume",
            reason="listener_backchannel",
            resume_text=resumable_text(),
        )
    if intent == "hold":
        return InterruptionResolution(action="hold", reason="candidate_hold")
    return InterruptionResolution(action="forward", reason="substantive_barge_in")


def listening_acknowledgement(locale: str) -> str:
    return SCRIPT.LISTENING_ACK_VI if locale.lower().startswith("vi") else SCRIPT.LISTENING_ACK_EN


def hold_acknowledgement(locale: str) -> str:
    return SCRIPT.HOLD_ACK_VI if locale.lower().startswith("vi") else SCRIPT.HOLD_ACK_EN
