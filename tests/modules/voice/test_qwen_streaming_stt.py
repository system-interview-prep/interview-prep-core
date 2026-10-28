import asyncio

import httpx
import numpy as np
import pytest
from livekit import rtc
from livekit.agents import stt

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
