"""Environment parsing for LiveKit speech-to-text provider selection."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class STTProviderSettings:
    provider: str
    model: str
    base_url: str | None = None
    chunk_size_seconds: float = 1.0
    endpoint_silence_seconds: float = 0.8
    silence_rms_threshold: float = 0.008


def stt_provider_settings(env: Mapping[str, str] | None = None) -> STTProviderSettings:
    """Return validated STT settings without exposing credentials in errors."""
    values = os.environ if env is None else env
    provider = values.get("LIVEKIT_STT_PROVIDER", "assemblyai").strip().lower()

    if provider == "assemblyai":
        model = values.get("LIVEKIT_STT_MODEL", "assemblyai/universal-3-5-pro").strip()
        if not model.startswith("assemblyai/"):
            raise ValueError(
                "LIVEKIT_STT_MODEL must be an AssemblyAI model when LIVEKIT_STT_PROVIDER=assemblyai"
            )
        return STTProviderSettings(provider=provider, model=model)

    if provider == "qwen":
        base_url = values.get("QWEN_STT_BASE_URL", "").strip().rstrip("/")
        if not base_url:
            raise ValueError("QWEN_STT_BASE_URL is required when LIVEKIT_STT_PROVIDER=qwen")
        if base_url.endswith("/v1"):
            raise ValueError("QWEN_STT_BASE_URL must be the streaming server root, without /v1")
        model = values.get("QWEN_STT_MODEL", "Qwen/Qwen3-ASR-1.7B").strip()
        if not model:
            raise ValueError("QWEN_STT_MODEL cannot be empty")
        try:
            chunk_size_seconds = float(values.get("QWEN_STT_CHUNK_SIZE_SECONDS", "1.0"))
        except ValueError as error:
            raise ValueError("QWEN_STT_CHUNK_SIZE_SECONDS must be between 0.2 and 5 seconds") from error
        if not 0.2 <= chunk_size_seconds <= 5:
            raise ValueError("QWEN_STT_CHUNK_SIZE_SECONDS must be between 0.2 and 5 seconds")
        try:
            endpoint_silence_seconds = float(values.get("QWEN_STT_ENDPOINT_SILENCE_SECONDS", "0.8"))
        except ValueError as error:
            raise ValueError("QWEN_STT_ENDPOINT_SILENCE_SECONDS must be between 0.3 and 3 seconds") from error
        if not 0.3 <= endpoint_silence_seconds <= 3:
            raise ValueError("QWEN_STT_ENDPOINT_SILENCE_SECONDS must be between 0.3 and 3 seconds")
        try:
            silence_rms_threshold = float(values.get("QWEN_STT_SILENCE_RMS_THRESHOLD", "0.008"))
        except ValueError as error:
            raise ValueError("QWEN_STT_SILENCE_RMS_THRESHOLD must be between 0.001 and 0.05") from error
        if not 0.001 <= silence_rms_threshold <= 0.05:
            raise ValueError("QWEN_STT_SILENCE_RMS_THRESHOLD must be between 0.001 and 0.05")
        return STTProviderSettings(
            provider=provider,
            model=model,
            base_url=base_url,
            chunk_size_seconds=chunk_size_seconds,
            endpoint_silence_seconds=endpoint_silence_seconds,
            silence_rms_threshold=silence_rms_threshold,
        )

    raise ValueError("LIVEKIT_STT_PROVIDER must be 'assemblyai' or 'qwen'")
