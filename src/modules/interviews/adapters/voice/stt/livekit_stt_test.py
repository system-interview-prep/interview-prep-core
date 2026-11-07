"""Listen-only LiveKit job. No interview tables, Core calls, LLM or TTS."""

import asyncio
import contextlib
import json
import logging
import math
from dataclasses import asdict
from time import perf_counter

from livekit.agents import Agent, AgentSession, stt

from ..config.tuning import voice_tuning
from ..speech.language import normalize_transcript_for_locale
from .recognition import create_stt, recognition_language, recognition_options, recognition_room_options
from .stt_provider import stt_provider_settings
from .transcript_correction import correct_technical_transcript

logger = logging.getLogger(__name__)
TOPIC = "intervia.stt-test"
RUNTIME = "/admin/ai/speech/test/livekit/runtime"


async def drain_audio(audio, stream, stop: asyncio.Event, on_audio) -> None:
    """End provider input, then let the event consumer receive its last final."""

    async def feed():
        async for frame in audio:
            on_audio(frame)
            stream.push_frame(frame)

    feeder = asyncio.create_task(feed())
    stopper = asyncio.create_task(stop.wait())
    try:
        await asyncio.wait((feeder, stopper), return_when=asyncio.FIRST_COMPLETED)
        if feeder.done():
            feeder.result()  # Preserve audio-source errors.
        else:
            feeder.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await feeder
    finally:
        stopper.cancel()
        if not feeder.done():
            feeder.cancel()
        await asyncio.gather(feeder, stopper, return_exceptions=True)
        stream.end_input()


async def run_stt_test(ctx, client) -> None:
    # Credentials are verified by the API, not decoded without verification here.
    response = await client.post(f"{RUNTIME}/start", timeout=10)
    response.raise_for_status()
    config = response.json()
    if config["roomName"] != ctx.job.room.name:
        raise RuntimeError("STT test room does not match runtime credential")
    stop, done = asyncio.Event(), asyncio.Event()
    queue: asyncio.Queue = asyncio.Queue()
    started = perf_counter()
    audio_seconds, frame_sequence, interim_count = 0.0, 0, 0
    raw_finals, finals = [], []
    trace_delivered = True
    status = "stopped"
    settings, tuning = stt_provider_settings(), voice_tuning()
    locale, context = config["locale"], config.get("context", "")

    def emit(event, **fields):
        queue.put_nowait(
            {"event": event, "fields": {"elapsed_ms": round((perf_counter() - started) * 1000), **fields}}
        )

    async def post_events():
        nonlocal trace_delivered
        while True:
            event = await queue.get()
            try:
                response = await client.post(f"{RUNTIME}/events", json=event, timeout=5)
                response.raise_for_status()
            except Exception:
                trace_delivered = False
                logger.warning("Could not persist STT test event", exc_info=True)
            finally:
                queue.task_done()

    poster = asyncio.create_task(post_events())
    provider = None
    session = None

    async def publish(payload):
        await ctx.room.local_participant.publish_data(
            json.dumps({"testId": config["testId"], **payload}), reliable=True, topic=TOPIC
        )

    class ListenAgent(Agent):
        def __init__(self):
            super().__init__(instructions="Transcribe only. Never ask questions or evaluate answers.")

        async def stt_node(self, audio, model_settings):
            nonlocal audio_seconds, frame_sequence, interim_count, status
            stream = provider.stream(language=recognition_language(locale))

            def count(frame):
                nonlocal audio_seconds, frame_sequence
                audio_seconds += frame.samples_per_channel / frame.sample_rate
                frame_sequence += 1

            feeder = asyncio.create_task(drain_audio(audio, stream, stop, count))
            try:
                async for event in stream:
                    if event.type in {
                        stt.SpeechEventType.INTERIM_TRANSCRIPT,
                        stt.SpeechEventType.FINAL_TRANSCRIPT,
                    }:
                        if event.alternatives and event.alternatives[0].text.strip():
                            raw = event.alternatives[0].text
                            final = event.type == stt.SpeechEventType.FINAL_TRANSCRIPT
                            text = normalize_transcript_for_locale(
                                raw, locale=locale, context=context, is_final=final
                            )
                            if final:
                                raw_finals.append(raw)
                                finals.append(text)
                            else:
                                interim_count += 1
                            emit(
                                "stt_test_transcript",
                                kind="final" if final else "interim",
                                sequence=len(finals) if final else interim_count,
                                raw_text=raw,
                                text=text,
                                changed=raw != text,
                                decisions=[
                                    asdict(d)
                                    for d in correct_technical_transcript(
                                        raw, locale=locale, context=context, is_final=final
                                    ).decisions
                                ],
                                audio_received_ms=round(audio_seconds * 1000),
                                audio_frame_sequence=frame_sequence,
                                confidence=None
                                if settings.provider == "qwen"
                                else event.alternatives[0].confidence,
                            )
                            await publish({"type": "final" if final else "interim", "text": text})
                    yield event
                await feeder
            except Exception:
                status = "provider_error"
                raise
            finally:
                feeder.cancel()
                await asyncio.gather(feeder, return_exceptions=True)
                try:
                    await stream.aclose()
                finally:
                    done.set()
                    stop.set()

    @ctx.room.on("data_received")
    def on_data(packet):
        if (
            packet.topic != TOPIC
            or not packet.participant
            or packet.participant.identity != config["identity"]
        ):
            return
        try:
            data = json.loads(packet.data)
            if data.get("type") == "stop":
                stats = data.get("stats")
                if isinstance(stats, dict):
                    emit(
                        "stt_test_transport_stats",
                        **{
                            key: value
                            for key, value in stats.items()
                            if key
                            in {
                                "packets_sent",
                                "bytes_sent",
                                "packets_lost",
                                "jitter_seconds",
                                "round_trip_seconds",
                            }
                            and isinstance(value, (int, float))
                            and math.isfinite(value)
                        },
                    )
                emit("stt_test_input_ended", reason="stop", audio_received_ms=round(audio_seconds * 1000))
                stop.set()
        except (ValueError, AttributeError):
            return

    @ctx.room.on("participant_disconnected")
    def on_left(participant):
        nonlocal status
        if participant.identity == config["identity"] and not stop.is_set():
            status = "disconnected"
            stop.set()

    def on_error(event):
        nonlocal status
        if not getattr(getattr(event, "error", None), "recoverable", False):
            status = "provider_error"
            stop.set()

    async def shutdown(_reason):
        stop.set()

    ctx.add_shutdown_callback(shutdown)
    try:
        await asyncio.wait_for(ctx.connect(), 10)
        provider = create_stt(
            settings,
            recognition_language(locale),
            context,
            telemetry=lambda fields: emit("stt_test_asr_diagnostic", **fields),
        )
        session = AgentSession(stt=provider, **recognition_options(settings, tuning))
        session.on("error", on_error)
        emit(
            "stt_test_started",
            mode="livekit",
            provider=settings.provider,
            model=settings.model,
            locale=locale,
            context=context,
            context_applied_to_asr=settings.provider != "qwen",
            language=recognition_language(locale),
            chunk_size_seconds=getattr(settings, "chunk_size_seconds", None),
            endpoint_silence_seconds=getattr(settings, "endpoint_silence_seconds", None),
            silence_rms_threshold=getattr(settings, "silence_rms_threshold", None),
            vad=asdict(tuning),
        )
        await asyncio.wait_for(
            session.start(
                room=ctx.room,
                agent=ListenAgent(),
                room_options=recognition_room_options(tuning, participant_identity=config["identity"]),
            ),
            20,
        )
        await publish(
            {
                "type": "ready",
                "provider": settings.provider,
                "model": settings.model,
                "traceEnabled": config["traceEnabled"],
            }
        )
        try:
            await asyncio.wait_for(stop.wait(), 20 * 60)
        except TimeoutError:
            status = "time_limit"
            stop.set()
        try:
            await asyncio.wait_for(done.wait(), 20)
        except TimeoutError:
            status = "drain_timeout"
    except asyncio.CancelledError:
        status = "cancelled"
        raise
    except Exception:
        status = "error"
        raise
    finally:
        stop.set()
        emit(
            "stt_test_completed",
            status=status,
            audio_received_ms=round(audio_seconds * 1000),
            audio_frame_sequence=frame_sequence,
            final_count=len(finals),
            interim_count=interim_count,
            raw_text=" ".join(raw_finals),
            text=" ".join(finals),
        )
        try:
            await asyncio.wait_for(queue.join(), 8)
        except TimeoutError:
            trace_delivered = False
        with contextlib.suppress(Exception):
            await publish(
                {
                    "type": "stopped",
                    "status": status,
                    "traceDelivered": trace_delivered,
                    "elapsedMs": round((perf_counter() - started) * 1000),
                }
            )
            # Give the reliable terminal packet time to reach the browser before
            # AgentSession deletes the room (same principle as interview release).
            await asyncio.sleep(0.5)
        poster.cancel()
        await asyncio.gather(poster, return_exceptions=True)
        if session is not None:
            with contextlib.suppress(Exception):
                await session.aclose()
        if provider is not None:
            with contextlib.suppress(Exception):
                await provider.aclose()
        await client.aclose()
        ctx.shutdown(reason=f"STT test {status}")
