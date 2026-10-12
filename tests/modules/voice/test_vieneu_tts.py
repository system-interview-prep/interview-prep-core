from __future__ import annotations

import asyncio
import base64
import json

import httpx

from src.modules.interviews.adapters.voice.vieneu_tts import VieNeuTTS


def test_vieneu_sse_audio_is_forwarded_as_pcm_frames() -> None:
    pcm_data = b"\x01\x00" * 320
    parts = [pcm_data[:31], pcm_data[31:80], pcm_data[80:]]
    body = "".join(
        f"data: {json.dumps({'audio': base64.b64encode(part).decode()})}\n\n" for part in parts
    ) + "data: [DONE]\n\n"

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["input"] == "Xin chào"
        assert payload["response_format"] == "pcm"
        assert payload["stream_format"] == "sse"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async def run() -> None:
        provider = VieNeuTTS(base_url="http://vieneu.test")
        await provider._client.aclose()
        provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        stream = provider.synthesize("Xin chào")
        frame = await stream.collect()
        assert frame.sample_rate == 16_000
        assert frame.num_channels == 1
        assert bytes(frame.data) == b"\x01\x00" * 320
        await stream.aclose()
        await provider.aclose()

    asyncio.run(run())
