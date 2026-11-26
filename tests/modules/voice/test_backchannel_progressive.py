"""Speech-end responsiveness (brief 2026-11-26): progressive PCM in the existing cache,
stable prefixes from successive interims, and revision checks before playback."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from src.modules.interviews.realtime import backchannel as bc
from src.modules.interviews.realtime.tts.cache import GrowingClip, SpeechAudioCache, SpeechClip

# ------------------------------------------------------------ stable prefix


def test_the_stable_prefix_is_what_two_interims_agree_on() -> None:
    assert bc.stable_prefix("em dùng redis làm cache", "em dùng redis làm cache và đặt") == "em dùng redis làm cache"
    assert bc.stable_prefix("em dùng redis làm cash", "em dùng redis làm cache và") == "em dùng redis làm"
    assert bc.stable_prefix("", "em dùng") == ""
    assert bc.stable_prefix("em dùng", "Em DÙNG redis") == "Em DÙNG"


def test_revisions_replacements_and_negations_are_caught_not_only_drift() -> None:
    written_for = "em dùng redis làm cache và đặt ttl năm phút"
    assert bc.transcript_revised(written_for, written_for + " cho từng key") is None
    # One re-recognized token is tolerated...
    assert bc.transcript_revised(written_for, "em dùng redis làm cash và đặt ttl năm phút cho") is None
    # ...two are a revision, whatever the word count.
    assert bc.transcript_revised(written_for, "em dùng mongo làm cash và đặt ttl năm phút cho") == "transcript_revised"
    # A negation that appeared in the covered stretch flips the meaning.
    assert bc.transcript_revised(written_for, "em không dùng redis làm cache và đặt ttl năm phút") == "transcript_revised"
    assert bc.transcript_revised("em dùng redis", "em dùng redis không") is None  # after the stretch: drift only
    assert bc.transcript_revised("em không dùng redis", "em có dùng redis thêm") == "transcript_revised"
    assert bc.transcript_revised(written_for, "em dùng redis") == "transcript_shrank"
    assert bc.transcript_revised("em dùng redis", "em dùng redis " + " ".join(["và"] * 13)) == "context_moved"
    assert bc.transcript_revised("", "anything") is None


# ------------------------------------------------------------ growing clip


@pytest.mark.asyncio
async def test_a_growing_clip_plays_what_has_arrived_and_waits_for_the_rest() -> None:
    clip = GrowingClip(sample_rate=16000, channels=1)
    clip.append(b"\x01\x00" * 480, now=1.0)  # 30 ms
    assert clip.audio_ms == 30 and clip.first_pcm_at == 1.0 and not clip.finished
    got: list[int] = []

    async def play():
        async for piece in clip.stream(frame_ms=20):
            got.append(len(piece))

    task = asyncio.create_task(play())
    await asyncio.sleep(0.02)
    assert got == [640], "one full 20 ms frame; the 10 ms remainder waits for more PCM"
    clip.append(b"\x02\x00" * 320, now=1.1)  # +20 ms -> 30 ms buffered
    await asyncio.sleep(0.02)
    assert got == [640, 640]
    clip.finish(now=1.2)
    await task
    assert got == [640, 640, 320], "the tail is flushed at the end"
    assert clip.underruns >= 1 and clip.finished and not clip.failed
    frozen = clip.to_clip()
    assert isinstance(frozen, SpeechClip) and len(frozen.pcm) == 1600


def test_the_cache_keeps_a_growing_clip_in_memory_and_forgets_a_failed_one() -> None:
    cache = SpeechAudioCache(voice_signature="v", directory=None)
    clip = GrowingClip(sample_rate=16000, channels=1)
    cache.put("À, TTL theo key.", clip, persist=False)
    assert cache.get("À, TTL theo key.") is clip
    clip.finish(failed=True)
    assert cache.get("À, TTL theo key.") is None, "a failed synthesis is not replayed as silence"


# ----------------------------------------------------- progressive prepare


class _Frame:
    def __init__(self, data: bytes) -> None:
        self.data, self.sample_rate, self.num_channels = data, 16000, 1


class _SlowStream:
    """VieNeu as it is: first PCM early, the rest trickling in."""

    def __init__(self, text: str, log: list[str], *, pieces: int = 4, delay: float = 0.03) -> None:
        log.append(text)
        self._pieces, self._delay, self._i = pieces, delay, 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._i >= self._pieces:
            raise StopAsyncIteration
        self._i += 1
        await asyncio.sleep(self._delay)
        return SimpleNamespace(frame=_Frame(bytes([self._i, 0]) * 320))

    async def aclose(self) -> None:
        pass


@pytest.mark.asyncio
async def test_prepared_audio_is_ready_at_the_first_pcm_and_keeps_synthesizing_during_playback() -> None:
    from tests.modules.voice.test_backchannel_runtime import _agent

    agent, events, _client, _spoken = _agent()
    agent._speech_cache = SpeechAudioCache(voice_signature="vieneu|test", directory=None)
    synthesized: list[str] = []
    agent._prepare_tts = SimpleNamespace(synthesize=lambda text: _SlowStream(text, synthesized))

    ready = await agent._prepare_backchannel_audio("À, TTL theo từng key.")
    assert ready is True
    clip = agent._speech_cache.get("À, TTL theo từng key.")
    assert isinstance(clip, GrowingClip) and not clip.finished and clip.audio_ms == 20, "ready after the first piece, not the last"

    async def text_stream():
        yield "À, TTL theo từng key."

    frames = [f async for f in agent.tts_node(text_stream(), None)]
    assert len(frames) == 4 and synthesized == ["À, TTL theo từng key."], "played progressively, synthesized once"
    await agent._prepare_task
    finished = agent._speech_cache.get("À, TTL theo từng key.")
    assert isinstance(finished, SpeechClip) and len(finished.pcm) == 4 * 640, "frozen once complete"
    kinds = [e for e, _ in events.items]
    assert "voice_backchannel_synthesized" in kinds and "voice_backchannel_playout" in kinds
    playout = events.of("voice_backchannel_playout")[0]
    assert playout["complete"] is True and playout["audio_ms"] == 80 and playout["underruns"] >= 1


@pytest.mark.asyncio
async def test_a_synthesis_that_yields_nothing_is_not_prepared() -> None:
    from tests.modules.voice.test_backchannel_runtime import _agent

    class _Empty:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    agent, *_ = _agent()
    agent._speech_cache = SpeechAudioCache(voice_signature="vieneu|test", directory=None)
    agent._prepare_tts = SimpleNamespace(synthesize=lambda text: _Empty())
    assert await agent._prepare_backchannel_audio("À, TTL theo từng key.") is False
    assert agent._speech_cache.get("À, TTL theo từng key.") is None


# ------------------------------------------------------ coordinator check


@pytest.mark.asyncio
async def test_a_pending_line_is_revalidated_for_revisions_before_the_vad_stop_plays_it() -> None:
    async def talker(request):
        yield {"type": "unit", "text": "Ừ, mình hình dung được.", "kind": "reflective", "index": 1, "ttft_ms": 300, "at_ms": 420}
        yield {"type": "done", "kind": "reflective", "ttft_ms": 300, "total_ms": 500, "units": 1, "rejected": [], "reason": None}

    async def prepare(_text: str) -> bool:
        return True

    played: list[str] = []
    events: list[tuple[str, dict]] = []
    coordinator = bc.BackchannelCoordinator(
        talker, lambda text, kind: played.append(text) or SimpleNamespace(done=lambda: False),
        enabled=True, min_gap_seconds=0.0, prepare=prepare, on_event=lambda e, **f: events.append((e, f)),
    )
    coordinator.on_user_speaking()
    written_for = "em dùng redis làm cache và đặt ttl năm phút cho từng key"
    assert coordinator.observe(written_for, phase="listening")
    await asyncio.gather(coordinator._task)
    assert coordinator._pending is not None and coordinator._pending["prepared_for_text"] == written_for
    # Qwen re-recognized the stretch with a negation: the prepared line is stale.
    coordinator.note_transcript("em không dùng redis làm cache và đặt ttl năm phút cho từng key nữa")
    coordinator.on_user_stopped()
    assert played == []
    assert [f["reason"] for e, f in events if e == "backchannel_discarded"] == ["transcript_revised"]
