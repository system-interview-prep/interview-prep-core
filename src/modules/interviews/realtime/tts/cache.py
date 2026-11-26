"""Reuse synthesized interviewer speech instead of paying for it again.

ElevenLabs bills every character synthesized. Two kinds of speech repeat:

- fixed lines ("Mình đang xem câu trả lời…" after every answer, the silence
  re-prompt, the hold acknowledgement): identical in every session, so their
  audio is kept on disk and shared by all sessions of the same voice;
- the current question, replayed on "nhắc lại" or "alo": kept in memory for
  the session only, since it carries the candidate's name and context.

Plain PCM bytes, no LiveKit types, so this stays testable without the SDK.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import wave
from collections import OrderedDict
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

_SAMPLE_WIDTH = 2  # 16-bit PCM, as LiveKit audio frames carry it


@dataclass(frozen=True)
class SpeechClip:
    pcm: bytes
    sample_rate: int
    channels: int

    def chunks(self, frame_ms: int = 20) -> list[bytes]:
        """Split into playout-sized pieces (20 ms by default)."""
        step = max(1, self.sample_rate * frame_ms // 1000) * self.channels * _SAMPLE_WIDTH
        return [self.pcm[i : i + step] for i in range(0, len(self.pcm), step)]


class GrowingClip:
    """A clip still being synthesized: playback may start on what has arrived.

    Progressive PCM for a prepared backchannel (brief 2026-11-26): VieNeu
    streams PCM over SSE, so the first bytes are there ~0.45 s in while the
    whole line takes 2-3 s. ``append`` is called by the synthesizer as pieces
    arrive, ``finish`` when it is done (or failed); ``stream`` yields
    playout-sized pieces as they become available and waits for more --
    counting every wait as an underrun -- until ``finish``. Same sample
    format as :class:`SpeechClip`; ``to_clip`` freezes it once complete.
    """

    #: Hard bound on what one growing clip may hold (30 s of 16 kHz mono 16-bit).
    MAX_BYTES = 30 * 16000 * 2

    def __init__(self, *, sample_rate: int, channels: int, max_bytes: int | None = None) -> None:
        self.sample_rate = sample_rate
        self.channels = channels
        self._pieces: list[bytes] = []
        self._bytes = 0
        self._max_bytes = max(1, int(max_bytes or self.MAX_BYTES))
        self._finished = False
        self._failed = False
        self._event = asyncio.Event()
        # Consumer-side waits for PCM that had not arrived yet: *generator*
        # waits, not audible underruns -- whether the listener hears a gap
        # depends on the player's own buffer, which this class cannot see.
        self.underruns = 0
        self.wait_ms_total = 0
        self.wait_ms_max = 0
        self.first_pcm_at: float | None = None
        self.finished_at: float | None = None

    @property
    def audio_ms(self) -> int:
        return self._bytes * 1000 // (self.sample_rate * self.channels * _SAMPLE_WIDTH) if self.sample_rate else 0

    @property
    def finished(self) -> bool:
        return self._finished

    @property
    def failed(self) -> bool:
        return self._failed

    @property
    def has_pcm(self) -> bool:
        return self._bytes > 0

    @property
    def usable(self) -> bool:
        """Playable: has audio and did not fail (a finished clip or one still growing)."""
        return self._bytes > 0 and not self._failed

    def append(self, pcm: bytes, *, now: float | None = None) -> None:
        if not pcm or self._finished:
            return
        # Whole samples only: a producer piece that splits a sample would
        # shift every later sample by one byte.
        sample = _SAMPLE_WIDTH * max(1, self.channels)
        if len(pcm) % sample:
            pcm = pcm[: len(pcm) - len(pcm) % sample]
            if not pcm:
                return
        if self._bytes + len(pcm) > self._max_bytes:
            # Bounded memory: a line this long is not a backchannel; finish as failed.
            self.finish(failed=True, now=now)
            return
        if self.first_pcm_at is None:
            self.first_pcm_at = now
        self._pieces.append(pcm)
        self._bytes += len(pcm)
        self._event.set()

    def finish(self, *, failed: bool = False, now: float | None = None) -> None:
        self._finished = True
        self._failed = failed
        self.finished_at = now
        self._event.set()

    def chunks(self, frame_ms: int = 20) -> list[bytes]:
        """What has arrived so far, in playout-sized pieces (complete clips only, normally)."""
        return SpeechClip(pcm=b"".join(self._pieces), sample_rate=self.sample_rate, channels=self.channels).chunks(frame_ms)

    def to_clip(self) -> SpeechClip | None:
        """The frozen clip, only once synthesis finished without failing.

        A cancelled or failed synthesis never becomes a ``SpeechClip``: what
        it holds is a truncated line, and a cached truncated line would be
        replayed as if it were the whole one.
        """
        if not self._finished or self._failed or not self._pieces:
            return None
        return SpeechClip(pcm=b"".join(self._pieces), sample_rate=self.sample_rate, channels=self.channels)

    async def stream(self, frame_ms: int = 20) -> AsyncIterator[bytes]:
        """Playout pieces as they become available; ends when synthesis has finished."""
        step = max(1, self.sample_rate * frame_ms // 1000) * self.channels * _SAMPLE_WIDTH
        buffer = b""
        index = 0
        while True:
            while index < len(self._pieces):
                buffer += self._pieces[index]
                index += 1
                while len(buffer) >= step:
                    yield buffer[:step]
                    buffer = buffer[step:]
            if self._finished:
                break
            self._event.clear()
            if index >= len(self._pieces):
                self.underruns += 1
                started = asyncio.get_running_loop().time()
                await self._event.wait()
                waited = round((asyncio.get_running_loop().time() - started) * 1000)
                self.wait_ms_total += waited
                self.wait_ms_max = max(self.wait_ms_max, waited)
        if buffer:
            yield buffer


class SpeechAudioCache:
    def __init__(self, *, voice_signature: str, directory: Path | None, memory_size: int = 12) -> None:
        # Audio from one voice must never be replayed for another one.
        self._voice_signature = voice_signature
        self._directory = directory
        self._memory: OrderedDict[str, SpeechClip] = OrderedDict()
        self._memory_size = max(1, memory_size)
        self.hits = 0
        self.saved_characters = 0

    def _key(self, text: str) -> str:
        return hashlib.sha256(f"{self._voice_signature}\n{text.strip()}".encode()).hexdigest()

    def _path(self, key: str) -> Path | None:
        return self._directory / f"{key}.wav" if self._directory else None

    def get(self, text: str) -> "SpeechClip | GrowingClip | None":
        key = self._key(text)
        clip = self._memory.get(key)
        if isinstance(clip, GrowingClip) and (clip.failed or not clip.has_pcm):
            # Failed, cancelled, or not a byte yet: not something to replay.
            if clip.failed:
                self._memory.pop(key, None)
            return None
        if clip is None:
            clip = self._read(key)
            if clip is not None:
                self._remember(key, clip)
        else:
            self._memory.move_to_end(key)
        if clip is not None:
            self.hits += 1
            self.saved_characters += len(text)
        return clip

    def freeze(self, text: str, growing: "GrowingClip") -> bool:
        """Replace ``growing`` by its frozen clip -- only if it is still the entry.

        A producer that was superseded (the same line prepared again after
        eviction) must not overwrite the newer entry, and a cancelled or
        failed producer has nothing valid to freeze: then the entry is
        removed instead. Returns whether a frozen clip is now cached.
        """
        key = self._key(text)
        if self._memory.get(key) is not growing:
            return False
        frozen = growing.to_clip()
        if frozen is None:
            self._memory.pop(key, None)
            return False
        self._memory[key] = frozen
        self._memory.move_to_end(key)
        return True

    def forget(self, text: str, growing: "GrowingClip | None" = None) -> None:
        """Drop the entry for ``text`` (only if it is ``growing``, when given)."""
        key = self._key(text)
        if growing is None or self._memory.get(key) is growing:
            self._memory.pop(key, None)

    def put(self, text: str, clip: "SpeechClip | GrowingClip", *, persist: bool) -> None:
        if isinstance(clip, GrowingClip):
            # A clip still arriving: in memory only; its producer freezes it
            # through ``freeze`` when done.
            self._remember(self._key(text), clip)
            return
        if not clip.pcm:
            return
        key = self._key(text)
        self._remember(key, clip)
        if persist:
            self._write(key, clip)

    def _remember(self, key: str, clip: "SpeechClip | GrowingClip") -> None:
        self._memory[key] = clip
        self._memory.move_to_end(key)
        while len(self._memory) > self._memory_size:
            self._memory.popitem(last=False)

    def _read(self, key: str) -> SpeechClip | None:
        path = self._path(key)
        if path is None or not path.is_file():
            return None
        try:
            with wave.open(str(path), "rb") as source:
                return SpeechClip(
                    pcm=source.readframes(source.getnframes()),
                    sample_rate=source.getframerate(),
                    channels=source.getnchannels(),
                )
        except (OSError, wave.Error, EOFError):
            logger.warning("Ignoring unreadable cached speech %s", path)
            return None

    def _write(self, key: str, clip: SpeechClip) -> None:
        path = self._path(key)
        if path is None or path.exists():
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Write then rename, so a concurrent session never reads half a file.
            partial = path.with_suffix(f".{os.getpid()}.part")
            with wave.open(str(partial), "wb") as target:
                target.setnchannels(clip.channels)
                target.setsampwidth(_SAMPLE_WIDTH)
                target.setframerate(clip.sample_rate)
                target.writeframes(clip.pcm)
            partial.replace(path)
        except OSError:
            logger.warning("Could not cache speech at %s", path, exc_info=True)
