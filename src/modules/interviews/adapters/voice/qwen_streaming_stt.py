"""LiveKit streaming STT adapter for Qwen3-ASR's official chunk API."""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

import httpx
import numpy as np
from livekit.agents import (
    DEFAULT_API_CONNECT_OPTIONS,
    NOT_GIVEN,
    APIConnectionError,
    APIConnectOptions,
    stt,
)
from livekit.agents.types import NotGivenOr

logger = logging.getLogger(__name__)


class QwenStreamingSTT(stt.STT):
    """Send mono 16 kHz float PCM chunks to Qwen and emit interim transcripts."""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        chunk_size_seconds: float = 1.0,
        endpoint_silence_seconds: float = 0.8,
        silence_rms_threshold: float = 0.008,
    ) -> None:
        super().__init__(
            capabilities=stt.STTCapabilities(
                streaming=True,
                interim_results=True,
                offline_recognize=False,
            )
        )
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._chunk_size_seconds = chunk_size_seconds
        self._endpoint_silence_seconds = endpoint_silence_seconds
        self._silence_rms_threshold = silence_rms_threshold
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(connect=10.0, read=90.0, write=30.0, pool=10.0),
        )

    @property
    def model(self) -> str:
        return self._model

    @property
    def provider(self) -> str:
        return "qwen"

    async def _recognize_impl(
        self,
        buffer: Any,
        *,
        language: NotGivenOr[str] = NOT_GIVEN,
        conn_options: APIConnectOptions,
    ) -> stt.SpeechEvent:
        raise APIConnectionError("Qwen adapter only supports streaming recognition", retryable=False)

    def stream(
        self,
        *,
        language: NotGivenOr[str] = NOT_GIVEN,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> stt.RecognizeStream:
        return _QwenRecognizeStream(
            stt=self,
            conn_options=conn_options,
            sample_rate=16_000,
            language=language,
        )

    async def aclose(self) -> None:
        await self._client.aclose()


class _QwenRecognizeStream(stt.RecognizeStream):
    def __init__(
        self,
        *,
        stt: QwenStreamingSTT,
        conn_options: APIConnectOptions,
        sample_rate: int,
        language: NotGivenOr[str],
    ) -> None:
        super().__init__(stt=stt, conn_options=conn_options, sample_rate=sample_rate)
        self._qwen = stt
        self._language = language if language is not NOT_GIVEN else "vi"

    async def _run(self) -> None:
        session_id: str | None = None
        request_id = str(uuid.uuid4())
        pending = np.empty(0, dtype=np.float32)
        audio_samples = 0
        trailing_silence_samples = 0
        last_text = ""
        speech_started_at: float | None = None
        chunk_samples = max(1, round(self._qwen._chunk_size_seconds * 16_000))

        async def start_session() -> str:
            try:
                response = await self._qwen._client.post("/api/start")
                response.raise_for_status()
                value = response.json().get("session_id")
                if not value:
                    raise ValueError("Qwen streaming API returned no session_id")
                return str(value)
            except (httpx.HTTPError, ValueError) as error:
                raise APIConnectionError(f"Could not start Qwen streaming session: {error}") from error

        async def send_chunk(chunk: np.ndarray) -> str:
            assert session_id is not None
            try:
                response = await self._qwen._client.post(
                    "/api/chunk",
                    params={"session_id": session_id},
                    content=np.asarray(chunk, dtype="<f4").tobytes(),
                    headers={"Content-Type": "application/octet-stream"},
                )
                response.raise_for_status()
                return str(response.json().get("text") or "").strip()
            except (httpx.HTTPError, ValueError) as error:
                raise APIConnectionError(f"Qwen streaming chunk failed: {error}") from error

        def emit_interim(text: str) -> None:
            nonlocal last_text
            if text and text != last_text:
                last_text = text
                self._event_ch.send_nowait(
                    stt.SpeechEvent(
                        type=stt.SpeechEventType.INTERIM_TRANSCRIPT,
                        request_id=request_id,
                        alternatives=[
                            # LiveKit averages confidence internally and requires a number;
                            # Qwen's streaming endpoint does not provide a calibrated score.
                            stt.SpeechData(language=str(self._language), text=text, confidence=1.0)
                        ],
                    )
                )

        async def finish_session() -> None:
            nonlocal session_id, pending, audio_samples, last_text, speech_started_at
            if session_id is None:
                return
            if pending.size:
                emit_interim(await send_chunk(pending))
            try:
                response = await self._qwen._client.post("/api/finish", params={"session_id": session_id})
                response.raise_for_status()
                final_text = str(response.json().get("text") or "").strip()
            except (httpx.HTTPError, ValueError) as error:
                raise APIConnectionError(f"Qwen streaming finish failed: {error}") from error

            emit_interim(final_text)
            ended_at = time.time()
            self._event_ch.send_nowait(
                stt.SpeechEvent(
                    type=stt.SpeechEventType.END_OF_SPEECH,
                    request_id=request_id,
                    speech_start_time=speech_started_at,
                    speech_end_time=ended_at,
                )
            )
            if final_text:
                logger.info(
                    "Qwen utterance finalized after audio silence",
                    extra={
                        "audio_duration_ms": round(audio_samples / 16_000 * 1000),
                        "transcript_characters": len(final_text),
                    },
                )
                self._event_ch.send_nowait(
                    stt.SpeechEvent(
                        type=stt.SpeechEventType.FINAL_TRANSCRIPT,
                        request_id=request_id,
                        alternatives=[
                            stt.SpeechData(language=str(self._language), text=final_text, confidence=1.0)
                        ],
                        speech_start_time=speech_started_at,
                        speech_end_time=ended_at,
                    )
                )
            self._event_ch.send_nowait(
                stt.SpeechEvent(
                    type=stt.SpeechEventType.RECOGNITION_USAGE,
                    request_id=request_id,
                    recognition_usage=stt.RecognitionUsage(audio_duration=audio_samples / 16_000),
                )
            )
            session_id = None
            pending = np.empty(0, dtype=np.float32)
            audio_samples = 0
            last_text = ""
            speech_started_at = None

        async for item in self._input_ch:
            if isinstance(item, stt.RecognizeStream._FlushSentinel):
                await finish_session()
                request_id = str(uuid.uuid4())
                trailing_silence_samples = 0
                continue

            pcm = np.frombuffer(item.data, dtype=np.int16)
            if item.num_channels > 1:
                pcm = pcm.reshape(-1, item.num_channels).mean(axis=1)
            samples = pcm.astype(np.float32) / 32768.0
            is_silence = float(np.sqrt(np.mean(np.square(samples)))) < self._qwen._silence_rms_threshold
            if is_silence:
                trailing_silence_samples += samples.size
                # Do not create empty Qwen sessions while the candidate is idle.
                if session_id is None:
                    continue
            else:
                trailing_silence_samples = 0
                if session_id is None:
                    session_id = await start_session()
                    speech_started_at = time.time()
                    self._event_ch.send_nowait(
                        stt.SpeechEvent(
                            type=stt.SpeechEventType.START_OF_SPEECH,
                            request_id=request_id,
                            speech_start_time=speech_started_at,
                        )
                    )

            pending = np.concatenate((pending, samples))
            audio_samples += samples.size

            while pending.size >= chunk_samples:
                emit_interim(await send_chunk(pending[:chunk_samples]))
                pending = pending[chunk_samples:]

            if (
                session_id is not None
                and trailing_silence_samples / 16_000 >= self._qwen._endpoint_silence_seconds
            ):
                # VAD-based LiveKit turn detection does not flush the custom STT
                # stream. Close this Qwen utterance after trailing PCM silence so
                # /api/finish can emit a final transcript and release the server session.
                await finish_session()
                request_id = str(uuid.uuid4())
                trailing_silence_samples = 0

        # LiveKit normally sends a flush sentinel at end-of-speech, but a
        # stream can also close normally (for example when its audio track is
        # unpublished). Do not drop the last buffered audio or leave the Qwen
        # session without /api/finish in that case: AgentSession needs the
        # final transcript event to complete the user turn and call Core.
        await finish_session()
