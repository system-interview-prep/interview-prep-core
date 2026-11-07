import asyncio

import httpx
import numpy as np
import pytest
from livekit import rtc
from livekit.agents import APIConnectionError, stt

from src.modules.interviews.adapters.voice.qwen_streaming_stt import QwenStreamingSTT


@pytest.mark.asyncio
async def test_qwen_stream_finalizes_utterance_after_trailing_silence():
    requested_paths: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested_paths.append(request.url.path)
        if request.url.path == "/api/start":
            return httpx.Response(200, json={"session_id": "session-1"})
        if request.url.path == "/api/chunk":
            return httpx.Response(200, json={"text": "xin chào"})
        if request.url.path == "/api/finish":
            return httpx.Response(200, json={"text": "xin chào"})
        return httpx.Response(404)

    provider = QwenStreamingSTT(
        base_url="http://qwen.test",
        model="Qwen/Qwen3-ASR-1.7B",
        chunk_size_seconds=0.2,
        endpoint_silence_seconds=0.3,
        silence_rms_threshold=0.008,
    )
    await provider._client.aclose()
    provider._client = httpx.AsyncClient(
        base_url="http://qwen.test",
        transport=httpx.MockTransport(handler),
    )
    stream = provider.stream()

    async def receive_final() -> stt.SpeechEvent:
        async for event in stream:
            if event.type == stt.SpeechEventType.FINAL_TRANSCRIPT:
                return event
        raise AssertionError("Qwen stream ended without a final transcript")

    final_task = asyncio.create_task(receive_final())
    speech = (np.ones(6_400, dtype=np.int16) * 4_000).tobytes()
    silence = np.zeros(5_120, dtype=np.int16).tobytes()
    stream.push_frame(rtc.AudioFrame(speech, 16_000, 1, 6_400))
    stream.push_frame(rtc.AudioFrame(silence, 16_000, 1, 5_120))

    try:
        final = await asyncio.wait_for(final_task, timeout=2)
        assert final.alternatives[0].text == "xin chào"
        assert final.alternatives[0].confidence == 1.0
        assert "/api/finish" in requested_paths
    finally:
        await stream.aclose()
        await provider.aclose()


@pytest.mark.asyncio
async def test_slow_http_keeps_every_sample_in_order_and_measures_actual_pcm_end():
    chunks, diagnostics = [], []
    chunk_started, release_chunk = asyncio.Event(), asyncio.Event()

    async def handler(request):
        if request.url.path == "/api/start":
            return httpx.Response(200, json={"session_id": "one"})
        if request.url.path == "/api/chunk":
            chunks.append(np.frombuffer(request.content, dtype="<f4").copy())
            if len(chunks) == 1:
                chunk_started.set()
                await release_chunk.wait()
            return httpx.Response(200, json={"text": "partial"})
        await asyncio.sleep(0.04)
        return httpx.Response(200, json={"text": "complete answer"})

    provider = QwenStreamingSTT(
        base_url="http://qwen", model="fake", chunk_size_seconds=0.2, telemetry=diagnostics.append
    )
    await provider._client.aclose()
    provider._client = httpx.AsyncClient(base_url="http://qwen", transport=httpx.MockTransport(handler))
    stream = provider.stream()
    events = []

    async def consume():
        async for event in stream:
            events.append(event)

    consumer = asyncio.create_task(consume())
    frames = [np.full(3200, n, dtype=np.int16) for n in [1000, 2000, 3000, 4000]]
    try:
        stream.push_frame(rtc.AudioFrame(frames[0].tobytes(), 16000, 1, 3200))
        await asyncio.wait_for(chunk_started.wait(), 2)
        for frame in frames[1:]:
            stream.push_frame(rtc.AudioFrame(frame.tobytes(), 16000, 1, 3200))
        stream.end_input()
        release_chunk.set()
        await asyncio.wait_for(consumer, 3)
        actual = np.concatenate(chunks)
        expected = np.concatenate(frames).astype(np.float32) / 32768
        np.testing.assert_array_equal(actual, expected)
        assert any(d["backlog_ms"] > 0 for d in diagnostics)
        final = next(d for d in diagnostics if d["phase"] == "finish")
        assert final["audio_received_ms"] == final["audio_consumed_ms"] == final["submitted_audio_ms"] == 800
        assert final["backlog_ms"] == 0
        assert final["pcm_speech_end_to_final_ms"] >= 30
        assert final["processing_ms"] >= 30
        assert [d["sequence"] for d in diagnostics if d["phase"] == "chunk"] == [1, 2, 3, 4]
        end_index = next(i for i, e in enumerate(events) if e.type == stt.SpeechEventType.END_OF_SPEECH)
        final_index = next(i for i, e in enumerate(events) if e.type == stt.SpeechEventType.FINAL_TRANSCRIPT)
        assert end_index < final_index
        assert events[final_index].speech_end_time == events[end_index].speech_end_time
    finally:
        release_chunk.set()
        await stream.aclose()
        await provider.aclose()


@pytest.mark.asyncio
async def test_telemetry_failure_cannot_break_final_and_short_tail_is_flushed():
    submitted = []

    def broken_sink(fields):
        raise RuntimeError("trace offline")

    async def handler(request):
        if request.url.path == "/api/start":
            return httpx.Response(200, json={"session_id": "one"})
        if request.url.path == "/api/chunk":
            submitted.append(request.content)
        return httpx.Response(200, json={"text": "tail"})

    provider = QwenStreamingSTT(base_url="http://qwen", model="fake", telemetry=broken_sink, language="en")
    await provider._client.aclose()
    provider._client = httpx.AsyncClient(base_url="http://qwen", transport=httpx.MockTransport(handler))
    stream = provider.stream()
    try:
        tail = np.full(320, 4000, dtype=np.int16)
        stream.push_frame(rtc.AudioFrame(tail.tobytes(), 16000, 1, 320))
        stream.end_input()
        events = [e async for e in stream]
        assert len(submitted) == 1 and len(submitted[0]) == 320 * 4
        final = next(e for e in events if e.type == stt.SpeechEventType.FINAL_TRANSCRIPT)
        assert final.alternatives[0].text == "tail"
        assert final.alternatives[0].language == "en"
    finally:
        await stream.aclose()
        await provider.aclose()


@pytest.mark.asyncio
async def test_failed_chunk_releases_server_session_without_retrying_a_truncated_answer():
    paths, diagnostics = [], []

    async def handler(request):
        paths.append(request.url.path)
        if request.url.path == "/api/start":
            return httpx.Response(200, json={"session_id": "one"})
        if request.url.path == "/api/chunk":
            return httpx.Response(503)
        return httpx.Response(200, json={"text": "must not become a final after an error"})

    provider = QwenStreamingSTT(base_url="http://qwen", model="fake", telemetry=diagnostics.append)
    await provider._client.aclose()
    provider._client = httpx.AsyncClient(base_url="http://qwen", transport=httpx.MockTransport(handler))
    stream = provider.stream()
    try:
        stream.push_frame(rtc.AudioFrame(np.full(16000, 4000, dtype=np.int16).tobytes(), 16000, 1, 16000))
        stream.end_input()
        received = []
        with pytest.raises(APIConnectionError):
            async for event in stream:
                received.append(event)
        assert paths == ["/api/start", "/api/chunk", "/api/finish"]
        assert not any(e.type == stt.SpeechEventType.FINAL_TRANSCRIPT for e in received)
        assert diagnostics[-1]["phase"] == "aborted"
    finally:
        await stream.aclose()
        await provider.aclose()


@pytest.mark.asyncio
async def test_multiple_utterances_and_stop_tail_keep_separate_ids_without_duplicate_finals():
    starts, chunks, diagnostics = [], [], []

    async def handler(request):
        if request.url.path == "/api/start":
            starts.append(str(len(starts) + 1))
            return httpx.Response(200, json={"session_id": starts[-1]})
        if request.url.path == "/api/chunk":
            chunks.append(np.frombuffer(request.content, dtype="<f4").copy())
        return httpx.Response(200, json={"text": f"answer {request.url.params['session_id']}"})

    provider = QwenStreamingSTT(
        base_url="http://qwen",
        model="fake",
        chunk_size_seconds=0.2,
        endpoint_silence_seconds=0.3,
        telemetry=diagnostics.append,
    )
    await provider._client.aclose()
    provider._client = httpx.AsyncClient(base_url="http://qwen", transport=httpx.MockTransport(handler))
    stream = provider.stream()
    frames = [
        np.full(6400, 4000, dtype=np.int16),
        np.zeros(6400, dtype=np.int16),
        np.full(3200, 5000, dtype=np.int16),
    ]
    try:
        for frame in frames:
            stream.push_frame(rtc.AudioFrame(frame.tobytes(), 16000, 1, frame.size))
        stream.end_input()
        events = [event async for event in stream]
        finals = [e for e in events if e.type == stt.SpeechEventType.FINAL_TRANSCRIPT]
        assert [e.alternatives[0].text for e in finals] == ["answer 1", "answer 2"]
        assert finals[0].request_id != finals[1].request_id
        np.testing.assert_array_equal(
            np.concatenate(chunks), np.concatenate(frames).astype(np.float32) / 32768
        )
        assert diagnostics[-1]["phase"] == "finish"
        assert diagnostics[-1]["submitted_audio_ms"] == 1000
        assert len(starts) == 2
    finally:
        await stream.aclose()
        await provider.aclose()
