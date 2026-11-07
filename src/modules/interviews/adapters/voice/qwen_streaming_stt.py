"""LiveKit streaming STT adapter for Qwen3-ASR's official chunk API."""

from __future__ import annotations

import contextlib
import logging
import time
import uuid
from collections import deque
from collections.abc import Callable
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
        telemetry: Callable[[dict], None] | None = None,
        language: str = "vi",
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
        self._telemetry = telemetry
        self._language = language
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
        self._language = language if language is not NOT_GIVEN else stt._language
        self._received_seconds = 0.0
        self._processed_seconds = 0.0
        self._capture_markers: deque[tuple[float, float]] = deque()

    def push_frame(self, frame) -> None:
        # Capture before HTTP blocks consumption; keep metadata, not duplicate audio.
        super().push_frame(frame)
        self._received_seconds += frame.samples_per_channel / frame.sample_rate
        self._capture_markers.append((self._received_seconds, time.time()))

    def _emit_diagnostic(self, **fields) -> None:
        fields.update(
            audio_received_ms=round(self._received_seconds * 1000),
            audio_consumed_ms=round(self._processed_seconds * 1000),
            backlog_ms=max(0, round((self._received_seconds - self._processed_seconds) * 1000)),
        )
        logger.info("Qwen streaming diagnostic", extra=fields)
        if self._qwen._telemetry:
            try:
                self._qwen._telemetry(fields)
            except Exception:
                logger.warning("STT diagnostic sink failed", exc_info=True)

    async def _run(self) -> None:
        session_id: str | None = None
        request_id = str(uuid.uuid4())
        pending = np.empty(0, dtype=np.float32)
        audio_samples = 0
        trailing_silence_samples = 0
        last_text = ""
        speech_started_at: float | None = None
        last_voiced_at: float | None = None
        submitted_samples = 0
        chunk_sequence = 0
        chunk_samples = max(1, round(self._qwen._chunk_size_seconds * 16_000))

        async def start_session() -> str:
            started = time.perf_counter()
            try:
                response = await self._qwen._client.post("/api/start")
                response.raise_for_status()
                value = response.json().get("session_id")
                if not value:
                    raise ValueError("Qwen streaming API returned no session_id")
                self._emit_diagnostic(
                    phase="start",
                    request_id=request_id,
                    processing_ms=round((time.perf_counter() - started) * 1000),
                )
                return str(value)
            except (httpx.HTTPError, ValueError) as error:
                self._emit_diagnostic(phase="error", operation="start", request_id=request_id)
                raise APIConnectionError(
                    f"Could not start Qwen streaming session: {error}", retryable=False
                ) from error

        async def send_chunk(chunk: np.ndarray) -> str:
            nonlocal submitted_samples, chunk_sequence
            assert session_id is not None
            started = time.perf_counter()
            try:
                response = await self._qwen._client.post(
                    "/api/chunk",
                    params={"session_id": session_id},
                    content=np.asarray(chunk, dtype="<f4").tobytes(),
                    headers={"Content-Type": "application/octet-stream"},
                )
                response.raise_for_status()
                submitted_samples += chunk.size
                chunk_sequence += 1
                self._emit_diagnostic(
                    phase="chunk",
                    request_id=request_id,
                    sequence=chunk_sequence,
                    chunk_audio_ms=round(chunk.size / 16),
                    submitted_audio_ms=round(submitted_samples / 16),
                    processing_ms=round((time.perf_counter() - started) * 1000),
                )
                return str(response.json().get("text") or "").strip()
            except (httpx.HTTPError, ValueError) as error:
                self._emit_diagnostic(phase="error", operation="chunk", request_id=request_id)
                # A new /api/start cannot replay already consumed PCM. Retrying
                # _run would silently produce an answer missing its beginning.
                raise APIConnectionError(f"Qwen streaming chunk failed: {error}", retryable=False) from error

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
            nonlocal session_id, pending, audio_samples, last_text, speech_started_at, last_voiced_at
            if session_id is None:
                return
            ended_at = last_voiced_at
            # Mark PCM speech end before waiting for /api/finish.
            self._event_ch.send_nowait(
                stt.SpeechEvent(
                    type=stt.SpeechEventType.END_OF_SPEECH,
                    request_id=request_id,
                    speech_start_time=speech_started_at,
                    speech_end_time=ended_at,
                )
            )
            if pending.size:
                emit_interim(await send_chunk(pending))
            started = time.perf_counter()
            try:
                response = await self._qwen._client.post("/api/finish", params={"session_id": session_id})
                response.raise_for_status()
                final_text = str(response.json().get("text") or "").strip()
            except (httpx.HTTPError, ValueError) as error:
                self._emit_diagnostic(phase="error", operation="finish", request_id=request_id)
                raise APIConnectionError(f"Qwen streaming finish failed: {error}", retryable=False) from error

            self._emit_diagnostic(
                phase="finish",
                request_id=request_id,
                processing_ms=round((time.perf_counter() - started) * 1000),
                submitted_audio_ms=round(submitted_samples / 16),
                pcm_speech_end_to_final_ms=max(0, round((time.time() - ended_at) * 1000))
                if ended_at
                else None,
                speech_end_source="received_pcm_rms",
            )

            emit_interim(final_text)
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
            last_voiced_at = None

        try:
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
                self._processed_seconds += samples.size / 16_000
                received_at = self._capture_markers[0][1] if self._capture_markers else time.time()
                while self._capture_markers and self._capture_markers[0][0] <= self._processed_seconds + 1e-6:
                    _, received_at = self._capture_markers.popleft()
                is_silence = float(np.sqrt(np.mean(np.square(samples)))) < self._qwen._silence_rms_threshold
                if is_silence:
                    trailing_silence_samples += samples.size
                    # Do not create empty Qwen sessions while the candidate is idle.
                    if session_id is None:
                        continue
                else:
                    last_voiced_at = received_at
                    trailing_silence_samples = 0
                    if session_id is None:
                        session_id = await start_session()
                        speech_started_at = received_at
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
        finally:
            if session_id is not None:
                # Cancellation or a failed chunk must not strand GPU session state.
                # The normal success path has already cleared session_id.
                with contextlib.suppress(Exception):
                    await self._qwen._client.post(
                        "/api/finish", params={"session_id": session_id}, timeout=2.0
                    )
                self._emit_diagnostic(phase="aborted", request_id=request_id)
