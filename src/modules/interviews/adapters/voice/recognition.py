"""One recognition configuration for interviews, LiveKit tests and file tests."""

import logging
import os

from livekit.agents import TurnHandlingOptions, inference, room_io
from livekit.agents.voice.turn import EndpointingOptions

try:
    from .language import assemblyai_stream_options
    from .qwen_streaming_stt import QwenStreamingSTT
except ImportError:  # Worker is also launched as a standalone script.
    from language import assemblyai_stream_options
    from qwen_streaming_stt import QwenStreamingSTT


def recognition_language(locale: str) -> str:
    configured = os.getenv("LIVEKIT_STT_LANGUAGE", "session").strip().lower()
    if configured not in {"", "auto", "multi", "session"}:
        return configured
    return "vi" if locale.lower().startswith("vi") else "en"


def create_stt(settings, language: str, context: str = "", *, telemetry=None):
    if settings.provider == "qwen":
        # The official demo API does not accept context. Do not pretend that
        # posting an extra JSON field would enable contextual decoding.
        return QwenStreamingSTT(
            model=settings.model,
            base_url=settings.base_url or "",
            chunk_size_seconds=settings.chunk_size_seconds,
            endpoint_silence_seconds=settings.endpoint_silence_seconds,
            silence_rms_threshold=settings.silence_rms_threshold,
            telemetry=telemetry,
            language=language,
        )
    return inference.STT(
        model=settings.model,
        language=language,
        extra_kwargs=assemblyai_stream_options(language, agent_context=context or None),
    )


def recognition_options(settings, tuning, *, interruption=None) -> dict:
    turn = dict(
        turn_detection="stt" if settings.provider == "assemblyai" else "vad",
        endpointing=EndpointingOptions(
            mode="fixed",
            min_delay=0.0 if settings.provider == "assemblyai" else 0.8,
            max_delay=tuning.endpoint_max_delay,
        ),
    )
    if interruption is not None:
        turn["interruption"] = interruption
    return dict(
        vad=inference.VAD(
            model="silero",
            min_speech_duration=tuning.vad_min_speech_duration,
            min_silence_duration=tuning.vad_min_silence_duration,
            activation_threshold=tuning.vad_activation_threshold,
            deactivation_threshold=tuning.vad_deactivation_threshold,
        ),
        llm=None,
        aec_warmup_duration=tuning.aec_warmup_duration,
        min_consecutive_speech_delay=tuning.min_consecutive_speech_delay,
        turn_handling=TurnHandlingOptions(**turn),
    )


def recognition_room_options(tuning, *, participant_identity=None):
    noise_filter = None
    if tuning.noise_cancellation != "off":
        try:
            from livekit.plugins import noise_cancellation

            noise_filter = noise_cancellation.BVC()
        except ImportError:
            logging.getLogger(__name__).warning("BVC unavailable; using browser WebRTC filtering")
    options = room_io.RoomOptions(
        audio_input=room_io.AudioInputOptions(noise_cancellation=noise_filter, auto_gain_control=True),
        delete_room_on_close=True,
    )
    if participant_identity is not None:
        options.participant_identity = participant_identity
        # Test jobs explicitly drain their final before closing on disconnect.
        options.close_on_disconnect = False
    return options
