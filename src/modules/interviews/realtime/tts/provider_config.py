"""Pure provider configuration used by the realtime adapter and golden tests."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Literal, cast


def _number(env: Mapping[str, str], name: str, default: float) -> float:
    try:
        return float(env.get(name, str(default)))
    except ValueError:
        return default


def _flag(env: Mapping[str, str], name: str, default: bool) -> bool:
    value = env.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class TTSProviderConfig:
    model: str
    language: str | None
    stability: float
    similarity_boost: float
    style: float
    speed: float
    use_speaker_boost: bool
    auto_mode: bool
    apply_text_normalization: Literal["auto", "off", "on"]

    def telemetry(self) -> dict[str, object]:
        return asdict(self)


def tts_provider_config(
    locale: str, env: Mapping[str, str] | None = None
) -> TTSProviderConfig:
    values = os.environ if env is None else env
    normalization = values.get("ELEVENLABS_TEXT_NORMALIZATION", "auto").strip().lower()
    if normalization not in {"auto", "off", "on"}:
        normalization = "auto"
    is_vietnamese = locale.lower().startswith("vi")
    language_mode = values.get("ELEVENLABS_LANGUAGE_MODE", "auto").strip().lower()
    if language_mode not in {"auto", "locale"}:
        language_mode = "auto"
    # Pinning language_code=vi makes the normalizer pronounce embedded English
    # technical terms as Vietnamese. In auto mode ElevenLabs infers language
    # from the complete utterance while the Vietnamese voice preserves accent.
    language = None if is_vietnamese and language_mode == "auto" else ("vi" if is_vietnamese else "en")
    return TTSProviderConfig(
        model=values.get("ELEVENLABS_MODEL_ID", "eleven_flash_v2_5").strip()
        or "eleven_flash_v2_5",
        language=language,
        stability=_number(values, "ELEVENLABS_STABILITY", 0.45),
        similarity_boost=_number(values, "ELEVENLABS_SIMILARITY_BOOST", 0.75),
        style=_number(values, "ELEVENLABS_STYLE", 0.0),
        speed=_number(values, "ELEVENLABS_SPEED", 0.98),
        use_speaker_boost=_flag(values, "ELEVENLABS_SPEAKER_BOOST", True),
        # session.say supplies complete interview messages, so ElevenLabs can
        # bypass its text chunk schedule without sacrificing partial-LLM context.
        auto_mode=_flag(values, "ELEVENLABS_AUTO_MODE", True),
        apply_text_normalization=cast(Literal["auto", "off", "on"], normalization),
    )
