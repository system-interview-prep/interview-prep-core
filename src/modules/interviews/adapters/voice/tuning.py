"""Validated voice transport tuning independent from LiveKit imports."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass


def _number(
    values: Mapping[str, str],
    name: str,
    default: float,
    *,
    minimum: float,
    maximum: float,
) -> float:
    try:
        value = float(values.get(name, str(default)))
    except (TypeError, ValueError):
        return default
    return min(maximum, max(minimum, value))


@dataclass(frozen=True)
class VoiceTuning:
    vad_min_speech_duration: float
    vad_min_silence_duration: float
    vad_activation_threshold: float
    vad_deactivation_threshold: float
    endpoint_max_delay: float
    interruption_min_duration: float
    false_interruption_timeout: float
    aec_warmup_duration: float
    min_consecutive_speech_delay: float
    noise_cancellation: str


def voice_tuning(values: Mapping[str, str] | None = None) -> VoiceTuning:
    """Return safe defaults while containing malformed deployment settings."""
    env = os.environ if values is None else values
    activation = _number(
        env, "VOICE_LAB_VAD_ACTIVATION_THRESHOLD", 0.5, minimum=0.1, maximum=0.95
    )
    deactivation = _number(
        env, "VOICE_LAB_VAD_DEACTIVATION_THRESHOLD", 0.35, minimum=0.05, maximum=0.9
    )
    # Hysteresis avoids rapid speech/no-speech flapping around a single noisy threshold.
    deactivation = min(deactivation, max(0.05, activation - 0.05))
    noise_mode = str(env.get("VOICE_LAB_NOISE_CANCELLATION", "bvc")).strip().lower()
    if noise_mode not in {"bvc", "off"}:
        noise_mode = "bvc"
    return VoiceTuning(
        vad_min_speech_duration=_number(
            env, "VOICE_LAB_VAD_MIN_SPEECH_DURATION", 0.2, minimum=0.1, maximum=1.0
        ),
        vad_min_silence_duration=_number(
            env, "VOICE_LAB_VAD_MIN_SILENCE_DURATION", 0.45, minimum=0.25, maximum=2.0
        ),
        vad_activation_threshold=activation,
        vad_deactivation_threshold=deactivation,
        endpoint_max_delay=_number(
            env, "VOICE_LAB_ENDPOINT_MAX_DELAY", 1.8, minimum=0.5, maximum=5.0
        ),
        interruption_min_duration=_number(
            env, "VOICE_LAB_INTERRUPTION_MIN_DURATION", 0.7, minimum=0.2, maximum=2.0
        ),
        false_interruption_timeout=_number(
            env, "VOICE_LAB_FALSE_INTERRUPTION_TIMEOUT", 2.0, minimum=0.5, maximum=5.0
        ),
        aec_warmup_duration=_number(
            env, "VOICE_LAB_AEC_WARMUP_DURATION", 2.0, minimum=0.0, maximum=5.0
        ),
        min_consecutive_speech_delay=_number(
            env, "VOICE_LAB_MIN_CONSECUTIVE_SPEECH_DELAY", 0.18, minimum=0.0, maximum=1.0
        ),
        noise_cancellation=noise_mode,
    )
