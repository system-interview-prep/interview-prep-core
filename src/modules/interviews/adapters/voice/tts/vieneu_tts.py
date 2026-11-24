"""LiveKit TTS adapter for the self-hosted VieNeu OpenAI-compatible API.

VieNeu exposes a sentence request as an SSE stream whose ``audio`` fields are
base64-encoded PCM pieces. This adapter forwards those PCM bytes to LiveKit
incrementally, so playout can begin before the complete response is generated.
"""

from __future__ import annotations

import base64
import json
import logging
import uuid

import httpx
from livekit.agents import tts
from livekit.agents._exceptions import (
    APIConnectionError,
    APIError,
    APIStatusError,
    APITimeoutError,
)
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, APIConnectOptions

logger = logging.getLogger(__name__)


class VieNeuTTS(tts.TTS):
    """VieNeu TTS using the API's SSE PCM audio stream."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None = None,
        voice: str | None = None,
        model: str | None = None,
        timeout_seconds: float = 180.0,
        sample_rate: int = 16000,
    ) -> None:
        super().__init__(
            # VieNeu streams audio bytes for each request, but it does not
            # accept incremental text over one request.  Marking this false
            # makes LiveKit's StreamAdapter split complete text by sentence;
            # each sentence is then played progressively from our SSE stream.
            capabilities=tts.TTSCapabilities(streaming=False),
            sample_rate=sample_rate,
            num_channels=1,
        )
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key.strip() if api_key else ""
        self._voice = voice.strip() if voice else ""
        self._model = model.strip() if model else ""
        self._timeout_seconds = max(5.0, timeout_seconds)
        headers = {"Accept": "text/event-stream"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        self._client = httpx.AsyncClient(headers=headers, follow_redirects=True)

    @property
    def model(self) -> str:
        return self._model or "vieneu-tts"

    @property
    def provider(self) -> str:
        return "VieNeu"

    def synthesize(
        self,
        text: str,
        *,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> tts.ChunkedStream:
        return _VieNeuChunkedStream(tts=self, input_text=text, conn_options=conn_options)

    async def aclose(self) -> None:
        await self._client.aclose()


class _VieNeuChunkedStream(tts.ChunkedStream):
    def __init__(
        self,
        *,
        tts: VieNeuTTS,
        input_text: str,
        conn_options: APIConnectOptions,
    ) -> None:
        super().__init__(tts=tts, input_text=input_text, conn_options=conn_options)
        self._tts: VieNeuTTS = tts

    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        payload: dict[str, str] = {
            "input": self._input_text,
            "response_format": "pcm",
            "stream_format": "sse",
            "sample_rate": str(self._tts.sample_rate),
        }
        if self._tts._voice:
            payload["voice"] = self._tts._voice
        if self._tts._model:
            payload["model"] = self._tts._model

        timeout = httpx.Timeout(
            connect=min(self._conn_options.timeout, self._tts._timeout_seconds),
            read=None,
            write=self._tts._timeout_seconds,
            pool=self._tts._timeout_seconds,
        )
        initialized = False
        audio_bytes = 0

        try:
            async with self._tts._client.stream(
                "POST",
                f"{self._tts._base_url}/v1/audio/speech",
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=timeout,
            ) as response:
                if response.status_code >= 400:
                    body = (await response.aread())[:2000].decode("utf-8", errors="replace")
                    raise APIStatusError(
                        f"VieNeu TTS returned HTTP {response.status_code}",
                        status_code=response.status_code,
                        request_id=response.headers.get("x-request-id"),
                        body=body,
                        retryable=response.status_code == 429 or response.status_code >= 500,
                    )

                async for line in response.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    event_text = line[5:].strip()
                    if event_text == "[DONE]":
                        break
                    try:
                        event = json.loads(event_text)
                    except json.JSONDecodeError as exc:
                        raise APIError("VieNeu returned malformed SSE data", body=event_text) from exc

                    if isinstance(event, dict) and event.get("error"):
                        raise APIError(f"VieNeu TTS error: {event['error']}")
                    encoded_audio = event.get("audio") if isinstance(event, dict) else None
                    if not encoded_audio:
                        continue
                    try:
                        pcm = base64.b64decode(encoded_audio, validate=True)
                    except ValueError as exc:
                        raise APIError("VieNeu returned invalid base64 audio") from exc

                    if not pcm:
                        continue
                    if not initialized:
                        try:
                            sample_rate = int(
                                response.headers.get("x-sample-rate", str(self._tts.sample_rate))
                            )
                        except ValueError as exc:
                            raise APIError("VieNeu returned an invalid sample rate") from exc
                        output_emitter.initialize(
                            request_id=response.headers.get("x-request-id") or uuid.uuid4().hex,
                            sample_rate=sample_rate,
                            num_channels=1,
                            mime_type="audio/pcm",
                        )
                        initialized = True
                    output_emitter.push(pcm)
                    audio_bytes += len(pcm)

                if not initialized or audio_bytes == 0:
                    raise APIError("VieNeu returned no audio")
                # ChunkedStream.end_input() closes the emitter after _run
                # returns and marks the final frame. Calling flush() here as
                # well would create an unnecessary synthetic silence marker.
        except httpx.TimeoutException as exc:
            raise APITimeoutError("VieNeu TTS request timed out") from exc
        except httpx.HTTPError as exc:
            raise APIConnectionError("Could not connect to the VieNeu TTS API") from exc

