"""Language policy for the realtime voice adapter.

The interview session locale is authoritative.  Provider language metadata is
useful for observation, but must never be allowed to switch a Vietnamese
session to English after a short or technical utterance.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from typing import Any

_SHORT_VI_MISRECOGNITIONS = {
    "yeah": "Dạ.",
    "yah": "Dạ.",
    "ya": "Dạ.",
    "yep": "Dạ.",
    "yes": "Dạ.",
    "yeah yeah": "Dạ, dạ.",
    "yah yah": "Dạ, dạ.",
    "yep yep": "Dạ, dạ.",
    "uh huh": "Vâng.",
    "uh-huh": "Vâng.",
    "mm hmm": "Vâng.",
    "mhm": "Vâng.",
    "hello": "Alô.",
    "hello hello": "Alô, alô.",
}

# A Vietnamese sentence opened by "dạ" or "alô" is decoded as "Yeah, em nghe
# rõ" / "Hello, bạn nghe rõ không". Only the opening filler is rewritten, and
# only when the rest is Vietnamese; the answer itself is never touched.
_LEADING_VI_MISRECOGNITIONS = {"yeah": "Dạ", "yah": "Dạ", "yep": "Dạ", "yes": "Dạ", "hello": "Alô"}
_LEADING_FILLER = re.compile(r"^(yeah|yah|yep|yes|hello)\b[,.!?]?\s+(?=\S)", re.IGNORECASE)
_VIETNAMESE_LETTER = re.compile(
    "[àáạảãâầấậẩẫăằắặẳẵèéẹẻẽêềếệểễìíịỉĩòóọỏõôồốộổỗơờớợởỡùúụủũưừứựửữỳýỵỷỹđ]", re.IGNORECASE
)


def _phrase_key(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z-]+", " ", value.casefold())).strip()


def normalize_transcript_for_locale(text: str, *, locale: str) -> str:
    """Correct high-confidence short STT artifacts in a locale-locked session.

    This is deliberately exact-match only.  It fixes acknowledgements such as
    Vietnamese ``dạ`` being decoded as English ``yeah`` without rewriting real
    mixed-language technical answers.
    """

    cleaned = text.strip()
    if not locale.lower().startswith("vi"):
        return cleaned
    exact = _SHORT_VI_MISRECOGNITIONS.get(_phrase_key(cleaned))
    if exact is not None:
        return exact
    leading = _LEADING_FILLER.match(cleaned)
    if leading and _VIETNAMESE_LETTER.search(cleaned[leading.end():]):
        filler = _LEADING_VI_MISRECOGNITIONS[leading.group(1).casefold()]
        return f"{filler}, {cleaned[leading.end():]}"
    return cleaned


# Written forms a TTS voice reads badly: a Vietnamese voice says "CI gạch CD"
# and "C thăng". The transcript keeps the written form; only speech changes.
_SPOKEN_FORMS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bCI/CD\b", re.IGNORECASE), "CI CD"),
    (re.compile(r"\bUI/UX\b", re.IGNORECASE), "UI UX"),
    (re.compile(r"\bTCP/IP\b", re.IGNORECASE), "TCP IP"),
    (re.compile(r"\bI/O\b"), "I O"),
    (re.compile(r"\bA/B\b"), "A B"),
    (re.compile(r"(?<![\w#])C#", re.IGNORECASE), "C sharp"),
    (re.compile(r"(?<![\w.])\.NET\b", re.IGNORECASE), "dot NET"),
    (re.compile(r"\b(Node|Next|Vue|Nuxt|Express|Nest|Three)\.js\b", re.IGNORECASE), r"\1 JS"),
)
# "REST/GraphQL" between two Latin words: read the slash as "or", not "gạch".
_WORD_SLASH = re.compile(r"(?<=[A-Za-z0-9])/(?=[A-Za-z])")


# ---------------------------------------------------------------------------
# Numbers, units and layout read aloud. ElevenLabs recommends spelling numbers
# and symbols out before synthesis; a Vietnamese voice otherwise reads "25′"
# or "0,7" unpredictably. Only stand-alone numbers change: S3, HTTP2 and v2.5
# stay as written so they are read as names.
# ---------------------------------------------------------------------------
# --------------------------------------------------------------------------- #

_DIGITS_VI = ("không", "một", "hai", "ba", "bốn", "năm", "sáu", "bảy", "tám", "chín")


def _read_triple_vi(number: int, full: bool) -> str:
    hundreds, rest = divmod(number, 100)
    tens, units = divmod(rest, 10)
    words: list[str] = []
    if hundreds or full:
        words += [_DIGITS_VI[hundreds], "trăm"]
    if tens == 0:
        if units:
            if hundreds or full:
                words.append("linh")
            words.append(_DIGITS_VI[units])
    elif tens == 1:
        words.append("mười")
        if units:
            words.append("lăm" if units == 5 else _DIGITS_VI[units])
    else:
        words += [_DIGITS_VI[tens], "mươi"]
        if units == 1:
            words.append("mốt")
        elif units == 4:
            words.append("tư")
        elif units == 5:
            words.append("lăm")
        elif units:
            words.append(_DIGITS_VI[units])
    return " ".join(words)


def number_to_vietnamese(number: int) -> str:
    """Đọc số nguyên 0 – 999 999 999 bằng chữ tiếng Việt."""
    if number == 0:
        return "không"
    if number < 0 or number >= 1_000_000_000:
        return str(number)
    millions, rest = divmod(number, 1_000_000)
    thousands, units = divmod(rest, 1000)
    parts: list[str] = []
    if millions:
        parts.append(f"{_read_triple_vi(millions, False)} triệu")
    if thousands:
        parts.append(f"{_read_triple_vi(thousands, bool(millions))} nghìn")
    if units:
        parts.append(_read_triple_vi(units, bool(millions or thousands)))
    return " ".join(parts)


_MARKDOWN_RE = re.compile(r"(\*\*|__|`+)")
_BULLET_RE = re.compile(r"(?m)^\s*(?:[-*•]|\d+[.)])\s+")
_STANDALONE_INT_RE = re.compile(r"(?<![\w.,/:-])(\d{1,9})(?![\w/:]|[.,]\d|-\w)")
_DECIMAL_VI_RE = re.compile(r"(?<![\w.,])(\d{1,6}),(\d{1,3})(?![\w.,])")


def _spoken_layout(text_value: str, locale: str) -> str:
    """Đổi một câu hiển thị thành câu để đọc: bỏ ký hiệu, xuống dòng, đọc số.

    Chỉ đổi số đứng riêng (25, 0,7); số dính chữ như S3, HTTP2, v2.5 giữ
    nguyên để TTS đọc như tên riêng. Văn bản hiển thị trên màn hình không đổi.
    """
    is_vi = str(locale or "").lower().startswith("vi")
    spoken = _MARKDOWN_RE.sub("", str(text_value or ""))
    spoken = _BULLET_RE.sub("", spoken)
    # Xuống dòng là một nhịp nghỉ: thêm dấu chấm nếu câu trước chưa kết thúc.
    spoken = re.sub(r"([^\s.!?…:;,])\s*\n+\s*", r"\1. ", spoken)
    spoken = re.sub(r"\s*\n+\s*", " ", spoken)
    spoken = spoken.replace("→", ", ").replace("·", ", ")
    if is_vi:
        spoken = re.sub(r"(\d)\s*′", r"\1 phút", spoken)
        spoken = re.sub(r"(\d)\s*″", r"\1 giây", spoken)
        spoken = re.sub(r"(\d)\s*%", r"\1 phần trăm", spoken)
        spoken = _DECIMAL_VI_RE.sub(
            lambda m: f"{number_to_vietnamese(int(m.group(1)))} phẩy {' '.join(_DIGITS_VI[int(d)] for d in m.group(2))}"
            if len(m.group(2)) <= 2
            else m.group(0),
            spoken,
        )
        spoken = _STANDALONE_INT_RE.sub(lambda m: number_to_vietnamese(int(m.group(1))), spoken)
    else:
        spoken = re.sub(r"(\d)\s*′", r"\1 minutes", spoken)
        spoken = re.sub(r"(\d)\s*″", r"\1 seconds", spoken)
        spoken = re.sub(r"(\d)\s*%", r"\1 percent", spoken)
    spoken = re.sub(r"\s+([,.!?])(?=\s|$)", r"\1", spoken)
    spoken = re.sub(r",\s*,", ",", spoken)
    return re.sub(r"\s{2,}", " ", spoken).strip()


def speakable_text(text: str, *, locale: str) -> str:
    """Rewrite technical notation into what the interviewer voice should say."""

    spoken = _spoken_layout(text, locale)
    for pattern, replacement in _SPOKEN_FORMS:
        spoken = pattern.sub(replacement, spoken)
    conjunction = " hoặc " if locale.lower().startswith("vi") else " or "
    return re.sub(
        r"\S+",
        lambda token: token.group(0)
        if "://" in token.group(0) or token.group(0).startswith("www.")
        else _WORD_SLASH.sub(conjunction, token.group(0)),
        spoken,
    )


# Terms a candidate says in English with a Vietnamese accent ("bắc en", "đốc
# cơ", "rê đít"). The streaming model keeps them in English spelling when they
# are listed here; a free-text prompt did not. AssemblyAI accepts up to 100.
INTERVIEW_KEYTERMS: tuple[str, ...] = (
    # Vietnamese words the model otherwise hears as English ("hello", "yeah").
    "alô", "dạ", "vâng",
    # Languages, frameworks, platforms (taxonomy labels plus close relatives).
    "Python", "Java", "Kotlin", "JavaScript", "TypeScript", "C#", "PHP", "SQL", "HTML", "CSS",
    "Spring Boot", "FastAPI", "Django", "Node.js", ".NET", "React", "Angular", "Vue", "Next.js",
    "Android", "iOS", "Flutter", "PostgreSQL", "MySQL", "MongoDB", "Redis", "Kafka", "RabbitMQ",
    "Elasticsearch", "Docker", "Kubernetes", "AWS", "Azure", "Google Cloud", "Linux", "Nginx",
    "Git", "GitHub", "Jira", "Postman",
    # Everyday engineering vocabulary.
    "backend", "frontend", "full-stack", "API", "REST API", "GraphQL", "endpoint", "webhook",
    "database", "schema", "query", "index", "transaction", "cache", "microservices", "monolith",
    "service", "deploy", "rollback", "pipeline", "CI/CD", "unit test", "integration test",
    "refactor", "code review", "pull request", "merge", "branch", "bug", "debug", "logging",
    "monitoring", "performance", "latency", "throughput", "scale", "load balancer", "message queue",
    "event", "retry", "timeout", "authentication", "authorization", "JWT", "OAuth", "token",
    "refresh token", "session", "trade-off", "consistency", "availability", "deadline", "sprint",
    "Agile", "Scrum",
)


def assemblyai_session_options(
    language: str,
    *,
    agent_context: str | None = None,
    steering: str = "prompt",
) -> dict[str, Any]:
    """Return non-conflicting AssemblyAI options for a fixed session language.

    ``steering="keyterms"`` sends INTERVIEW_KEYTERMS; ``"prompt"`` sends the
    language-steering prompt. U3 Pro ignores the term list when both are set.
    """

    if steering == "keyterms":
        options: dict[str, Any] = {
            "keyterms_prompt": list(INTERVIEW_KEYTERMS),
            # Keep partial captions streaming while using the balanced profile
            # to improve recognition of ordinary speech.
            "mode": "balanced",
            "language_detection": False,
            "previous_context_n_turns": 2,
        }
        if agent_context and agent_context.strip():
            options["agent_context"] = agent_context.strip()
        return options

    language_name = "Vietnamese" if language == "vi" else "English"
    prompt = (
        f"The interview language is {language_name} ({language}). "
        f"Transcribe all ordinary speech as {language_name}. "
    )
    if language == "vi":
        prompt += (
            "Use Vietnamese spelling and diacritics. Short acknowledgements are "
            "Vietnamese words such as dạ, vâng, ừ, ừm, rồi, nhé and được; do not "
            "render them as yeah, yep, uh-huh or other English words. "
            "The Vietnamese call greeting alô (also written alo) is not the English "
            "hello: write alô. "
            "If the audio is silence, music, background conversation, or is not "
            "intelligible, do not invent a transcript or translate noise into English. "
        )
    prompt += (
        "Keep genuine software names and technical terms such as REST API, Docker, "
        "Kubernetes, JavaScript, PostgreSQL, Redis, cache, webhook, JWT, CI/CD and "
        "microservices verbatim."
    )
    options = {
        # U3 Pro documents `prompt` and `keyterms_prompt` as mutually exclusive.
        "prompt": prompt,
        "mode": "balanced",
        "language_detection": False,
        "previous_context_n_turns": 2,
    }
    if agent_context and agent_context.strip():
        # U3.6 uses the most recent agent utterance to disambiguate the next
        # user turn. This is context, not a transcript to copy.
        options["agent_context"] = agent_context.strip()
    return options


def _env_number(env: Mapping[str, str], name: str, default: float) -> float:
    try:
        return float(env.get(name, str(default)))
    except ValueError:
        return default


def assemblyai_stream_options(
    language: str,
    *,
    agent_context: str | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """The full AssemblyAI options of a live interview, shared with the STT benchmark."""

    values = os.environ if env is None else env
    steering = values.get("VOICE_LAB_STT_STEERING", "prompt").strip().lower()
    return {
        **assemblyai_session_options(
            language,
            agent_context=agent_context,
            steering=steering if steering in {"keyterms", "prompt"} else "prompt",
        ),
        # Keep natural pauses inside one answer instead of finalizing
        # every short clause as a separate turn.
        "mode": "balanced",
        "min_turn_silence": max(0, int(_env_number(values, "VOICE_LAB_STT_MIN_TURN_SILENCE_MS", 128))),
        "max_turn_silence": max(0, int(_env_number(values, "VOICE_LAB_STT_MAX_TURN_SILENCE_MS", 900))),
        "vad_threshold": _env_number(values, "VOICE_LAB_STT_VAD_THRESHOLD", 0.3),
    }
