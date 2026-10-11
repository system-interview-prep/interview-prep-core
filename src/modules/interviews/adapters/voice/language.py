"""Language policy for the realtime voice adapter.

The interview session locale is authoritative.  Provider language metadata is
useful for observation, but must never be allowed to switch a Vietnamese
session to English after a short or technical utterance.
"""

from __future__ import annotations

import re
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
}


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
    return _SHORT_VI_MISRECOGNITIONS.get(_phrase_key(cleaned), cleaned)


def assemblyai_session_options(
    language: str,
    *,
    agent_context: str | None = None,
) -> dict[str, Any]:
    """Return non-conflicting AssemblyAI options for a fixed session language."""

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
            "If the audio is silence, music, background conversation, or is not "
            "intelligible, do not invent a transcript or translate noise into English. "
        )
    prompt += (
        "Keep genuine software names and technical terms such as REST API, Docker, "
        "Kubernetes, JavaScript, PostgreSQL, JWT, CI/CD and microservices verbatim."
    )
    options: dict[str, Any] = {
        # U3 Pro documents `prompt` and `keyterms_prompt` as mutually exclusive.
        # A single language-steering prompt is more important than English-only
        # keyterms for a Vietnamese interview.
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
