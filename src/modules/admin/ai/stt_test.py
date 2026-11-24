"""Human STT checks with local traces, without creating interview sessions."""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import wave
from dataclasses import asdict
from time import perf_counter
from uuid import uuid4

from fastapi import HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from src.core.config import get_settings
from src.core.trace_logging import trace_event
from src.modules.interviews.adapters.voice.speech.language import (
    normalize_transcript_for_locale,
)
from src.modules.interviews.adapters.voice.stt.stt_provider import stt_provider_settings
from src.modules.interviews.adapters.voice.stt.transcript_correction import correct_technical_transcript

MAX_AUDIO_BYTES = 10 * 1024 * 1024


class SttTestOptions(BaseModel):
    context: str = Field(default="", max_length=4000)
    referenceText: str = Field(default="", max_length=16000)


class SttTestTrace:
    def __init__(self, mode: str, provider: str, model: str, locale: str) -> None:
        self.test_id = str(uuid4())
        self.provider, self.model, self.locale = provider, model, locale
        self.options = SttTestOptions()
        self.started_at = perf_counter()
        self.audio_samples = 0
        self.last_audio_at: float | None = None
        self.speech_end_at: float | None = None
        self.input_ended_at: float | None = None
        self.first_result_ms: int | None = None
        self.first_final_ms: int | None = None
        self.interim_count = 0
        self.raw_finals: list[str] = []
        self.finals: list[str] = []
        self.finished = False
        settings = get_settings()
        self.enabled = settings.trace_logs_enabled
        if self.enabled is None:
            self.enabled = settings.app_env.casefold() in {"development", "local"}
        self.emit("stt_test_started", mode=mode, provider=provider, model=model, locale=locale)

    def elapsed(self) -> int:
        return round((perf_counter() - self.started_at) * 1000)

    def emit(self, event: str, **fields) -> None:
        trace_event("test_stt", event, test_id=self.test_id, elapsed_ms=self.elapsed(), **fields)

    def configure(self, options: SttTestOptions) -> None:
        self.options = options
        # Reference text is only for later comparison. It is never sent to ASR
        # or used to choose replacement terms in the hypothesis.
        self.emit("stt_test_configured", context=options.context, reference_text=options.referenceText)

    def audio(self, samples: int) -> None:
        if not samples:
            return
        if not self.audio_samples:
            self.emit("stt_test_audio_started", sample_rate=16000, channels=1)
        self.audio_samples += samples
        self.last_audio_at = perf_counter()

    def speech_state(self, state: str) -> None:
        self.speech_end_at = perf_counter() if state == "ended" else None
        self.emit("stt_test_speech_state", state=state, audio_received_ms=round(self.audio_samples / 16))

    def input_ended(self, reason: str) -> None:
        self.input_ended_at = perf_counter()
        self.emit("stt_test_input_ended", reason=reason, audio_received_ms=round(self.audio_samples / 16))

    def transcript(self, raw: str, *, final: bool, confidence: float | None = None) -> dict:
        text = normalize_transcript_for_locale(
            raw, locale=self.locale, context=self.options.context, is_final=final
        )
        decisions = correct_technical_transcript(
            raw, locale=self.locale, context=self.options.context, is_final=final
        ).decisions
        now = perf_counter()
        if self.first_result_ms is None:
            self.first_result_ms = self.elapsed()
        if final:
            if self.first_final_ms is None:
                self.first_final_ms = self.elapsed()
            self.raw_finals.append(raw)
            self.finals.append(text)
        else:
            self.interim_count += 1
        self.emit(
            "stt_test_transcript",
            kind="final" if final else "interim",
            sequence=len(self.finals) if final else self.interim_count,
            raw_text=raw,
            text=text,
            changed=raw != text,
            decisions=[asdict(item) for item in decisions],
            confidence=None if self.provider == "qwen" else confidence,
            audio_received_ms=round(self.audio_samples / 16),
            after_last_audio_ms=round((now - self.last_audio_at) * 1000) if self.last_audio_at else None,
            speech_end_to_result_ms=(
                round((now - self.speech_end_at) * 1000) if final and self.speech_end_at else None
            ),
            input_end_to_result_ms=(
                round((now - self.input_ended_at) * 1000) if final and self.input_ended_at else None
            ),
        )
        if final:
            self.speech_end_at = None
        return {"type": "final" if final else "interim", "text": text, "testId": self.test_id}

    def finish(self, status: str) -> None:
        if self.finished:
            return
        self.finished = True
        self.emit(
            "stt_test_completed",
            status=status,
            audio_received_ms=round(self.audio_samples / 16),
            first_result_ms=self.first_result_ms,
            first_final_ms=self.first_final_ms,
            final_count=len(self.finals),
            interim_count=self.interim_count,
            raw_text=" ".join(self.raw_finals),
            text=" ".join(self.finals),
        )


def _language() -> str:
    from src.modules.interviews.adapters.voice.stt.recognition import recognition_language

    return recognition_language("vi-VN")


def _provider(settings, language: str, context: str = ""):
    from src.modules.interviews.adapters.voice.stt.recognition import create_stt

    return create_stt(settings, language, context)


async def _events(stream, trace: SttTestTrace, send=None) -> None:
    from livekit.agents import stt

    async for event in stream:
        if event.type == stt.SpeechEventType.START_OF_SPEECH:
            trace.speech_state("started")
        elif event.type == stt.SpeechEventType.END_OF_SPEECH:
            trace.speech_state("ended")
        elif event.type in {stt.SpeechEventType.INTERIM_TRANSCRIPT, stt.SpeechEventType.FINAL_TRANSCRIPT}:
            if not event.alternatives or not event.alternatives[0].text.strip():
                continue
            alternative = event.alternatives[0]
            payload = trace.transcript(
                alternative.text,
                final=event.type == stt.SpeechEventType.FINAL_TRANSCRIPT,
                confidence=getattr(alternative, "confidence", None),
            )
            if send:
                await send(payload)


async def transcribe_file(audio: UploadFile, options: SttTestOptions) -> dict:
    import numpy as np
    from livekit import rtc

    if audio.content_type not in {"audio/wav", "audio/x-wav", "audio/wave", "audio/vnd.wave"}:
        raise HTTPException(status_code=415, detail="Audio must be a WAV recording")
    data = await audio.read(MAX_AUDIO_BYTES + 1)
    if not data or len(data) > MAX_AUDIO_BYTES:
        raise HTTPException(status_code=413, detail="Audio must be between 1 byte and 10 MiB")
    try:
        with wave.open(io.BytesIO(data), "rb") as wav:
            if wav.getsampwidth() != 2 or wav.getcomptype() != "NONE":
                raise HTTPException(status_code=415, detail="WAV audio must use uncompressed 16-bit PCM")
            sample_rate, channels = wav.getframerate(), wav.getnchannels()
            samples = np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2")
    except (wave.Error, EOFError) as exc:
        raise HTTPException(status_code=415, detail="Invalid WAV audio") from exc
    if sample_rate <= 0 or channels <= 0 or samples.size == 0:
        raise HTTPException(status_code=422, detail="Audio is empty or has invalid format")
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1).astype(np.int16)
    if sample_rate != 16000:
        samples = np.interp(
            np.linspace(0, len(samples) - 1, max(1, round(len(samples) / sample_rate * 16000))),
            np.arange(len(samples)),
            samples,
        ).astype(np.int16)

    settings, language = stt_provider_settings(), _language()
    trace = SttTestTrace("file", settings.provider, settings.model, language)
    trace.configure(options)
    provider = stream = None
    task = None
    status = "completed"
    try:
        provider = _provider(settings, language, options.context)
        stream = provider.stream(language=language)
        task = asyncio.create_task(_events(stream, trace))
        trace.audio(samples.size)
        for offset in range(0, samples.size, 3200):
            chunk = samples[offset : offset + 3200]
            stream.push_frame(rtc.AudioFrame(chunk.tobytes(), 16000, 1, len(chunk)))
        # end_input flushes synchronously and allows all pending finals to drain.
        trace.input_ended("file_end")
        stream.end_input()
        await asyncio.wait_for(task, timeout=60)
    except TimeoutError as exc:
        status = "timeout"
        trace.emit("stt_test_error", error_type=type(exc).__name__)
        raise HTTPException(
            status_code=504, detail="Speech adapter did not return a transcript in time"
        ) from exc
    except BaseException as exc:
        status = "cancelled" if isinstance(exc, asyncio.CancelledError) else "error"
        trace.emit("stt_test_error", error_type=type(exc).__name__)
        raise
    finally:
        if task and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if stream:
            with contextlib.suppress(Exception):
                await stream.aclose()
        if provider:
            with contextlib.suppress(Exception):
                await provider.aclose()
        trace.finish(status)
    return {
        "text": " ".join(trace.finals),
        "elapsedMs": trace.elapsed(),
        "provider": settings.provider,
        "model": settings.model,
        "testId": trace.test_id,
        "traceEnabled": trace.enabled,
    }


async def transcribe_stream(websocket: WebSocket) -> None:
    from livekit import rtc

    provider = stream = trace = event_task = receive_task = None
    status, connected = "stopped", True

    async def send(payload: dict) -> None:
        nonlocal connected
        if connected:
            try:
                await websocket.send_json(payload)
            except (WebSocketDisconnect, RuntimeError, OSError):
                connected = False

    try:
        first = await websocket.receive()
        if first.get("type") == "websocket.disconnect":
            return
        options = SttTestOptions()
        if first.get("text"):
            config = json.loads(first["text"])
            if config.get("type") != "configure":
                raise ValueError("Expected STT test configuration")
            options = SttTestOptions.model_validate(config)
            first = None
        settings, language = stt_provider_settings(), _language()
        trace = SttTestTrace("stream", settings.provider, settings.model, language)
        trace.configure(options)
        provider = _provider(settings, language, options.context)
        stream = provider.stream(language=language)
        await send(
            {
                "type": "ready",
                "provider": settings.provider,
                "model": settings.model,
                "testId": trace.test_id,
                "traceEnabled": trace.enabled,
            }
        )
        event_task = asyncio.create_task(_events(stream, trace, send))
        received_bytes = 0
        while True:
            if first is not None:
                message, first = first, None
            else:
                receive_task = asyncio.create_task(websocket.receive())
                done, _ = await asyncio.wait({receive_task, event_task}, return_when=asyncio.FIRST_COMPLETED)
                if event_task in done:
                    await event_task  # Surface provider failure immediately, even during silence.
                    status = "provider_ended"
                    break
                message = receive_task.result()
            if message.get("type") == "websocket.disconnect":
                connected, status = False, "disconnected"
                break
            if message.get("bytes") is not None:
                pcm = message["bytes"]
                received_bytes += len(pcm)
                if received_bytes > MAX_AUDIO_BYTES or len(pcm) % 2:
                    raise ValueError("Expected 16-bit PCM audio up to 10 MiB")
                if pcm:
                    trace.audio(len(pcm) // 2)
                    stream.push_frame(rtc.AudioFrame(pcm, 16000, 1, len(pcm) // 2))
            elif message.get("text"):
                control = json.loads(message["text"])
                if control.get("type") == "stop":
                    break
                raise ValueError("Unexpected STT control message")
        trace.input_ended(status)
        stream.end_input()
        await asyncio.wait_for(event_task, timeout=15)
    except WebSocketDisconnect:
        connected, status = False, "disconnected"
    except asyncio.CancelledError:
        status = "cancelled"
        raise
    except Exception as exc:
        status = "timeout" if isinstance(exc, TimeoutError) else "error"
        if trace:
            trace.emit("stt_test_error", error_type=type(exc).__name__)
        await send({"type": "error", "detail": "STT test failed", "testId": trace.test_id if trace else None})
    finally:
        for task in (receive_task, event_task):
            if task and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        if stream:
            with contextlib.suppress(Exception):
                await stream.aclose()
        if provider:
            with contextlib.suppress(Exception):
                await provider.aclose()
        if trace:
            trace.finish(status)
        await send({"type": "stopped", "testId": trace.test_id if trace else None})
        with contextlib.suppress(Exception):
            await websocket.close()
