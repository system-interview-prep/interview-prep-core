"""One recognition configuration for interviews, LiveKit tests and file tests."""

import logging
import os

from livekit.agents import TurnHandlingOptions, inference, room_io
from livekit.agents.voice.turn import EndpointingOptions

from ..speech.language import assemblyai_stream_options
from .qwen import QwenStreamingSTT


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


#: With Qwen the final transcript arrives 1.25-1.7 s after VAD end of speech
#: (0.8 s of audio silence, up to 0.8 s of chunk backlog, a 0.3 s chunk and a
#: 0.45 s finish request; measured 2026-11-26). LiveKit's max endpointing delay
#: is how long it waits for that final before committing an empty turn, so it
#: must be above that, whatever the shared tuning says.
QWEN_ENDPOINT_MAX_DELAY_SECONDS = 3.0


def recognition_options(settings, tuning, *, interruption=None) -> dict:
    qwen = settings.provider != "assemblyai"
    turn = dict(
        turn_detection="stt" if settings.provider == "assemblyai" else "vad",
        endpointing=EndpointingOptions(
            mode="fixed",
            # No fixed delay after the final transcript for either provider.
            # Qwen used to get 0.8 s here, measured as a full 0.8 s on every
            # turn *after* its final had arrived: a duplicate of the answer
            # window's own silence wait (endpointing.py), which is the one
            # that merges fragments and the one the candidate's silence is
            # supposed to be measured by. The final transcript itself is what
            # LiveKit waits for; max_delay below bounds that wait.
            min_delay=0.0,
            max_delay=max(tuning.endpoint_max_delay, QWEN_ENDPOINT_MAX_DELAY_SECONDS) if qwen else tuning.endpoint_max_delay,
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
