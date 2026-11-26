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
    revised = bc.transcript_revised
    assert revised(written_for, written_for + " cho từng key") is None
    # Lexical and conservative: ONE changed word in the covered stretch is a revision.
    assert revised(written_for, "em dùng kafka làm cache và đặt ttl năm phút cho") == "transcript_revised"   # Redis -> Kafka
    assert revised(written_for, "em dùng redis làm cache và đặt ttl mười phút") == "transcript_revised"      # a number
    assert revised(written_for, "em dùng redis làm queue và đặt ttl năm phút") == "transcript_revised"       # a term
    assert revised(written_for, "em dùng redis làm cash và đặt ttl năm phút cho") == "transcript_revised"    # a re-recognized token
    # Insertion and deletion shift the stretch: revisions too.
    assert revised(written_for, "em có dùng redis làm cache và đặt ttl năm phút") == "transcript_revised"
    assert revised(written_for, "em dùng redis cache và đặt ttl năm phút cho từng") == "transcript_revised"
    # Case and punctuation are not revisions.
    assert revised(written_for, "Em dùng Redis, làm cache và đặt TTL năm phút.") is None
    # A negation inside the stretch, with or without punctuation.
    assert revised(written_for, "em không dùng redis làm cache và đặt ttl năm phút") == "transcript_revised"
    assert revised("em không dùng redis", "em không, dùng redis") is None  # punctuation only
    assert revised("em không dùng redis", "em dùng không redis") == "transcript_revised"  # the negation moved
    assert revised("em không dùng redis", "em có dùng redis thêm") == "transcript_revised"
    # A negation or self-correction right after the stretch reverses it.
    assert revised("em dùng redis", "em dùng redis không phải") == "negation_appended"
    assert revised("em dùng redis", "em dùng redis à nhầm kafka") == "self_correction"
    assert revised("i use redis", "i use redis, don't i") == "negation_appended"
    assert revised("i use redis", "i use redis. Actually Kafka.") == "self_correction"
    # Far after the stretch a negation is just the candidate going on.
    assert revised("em dùng redis", "em dùng redis cho cache còn queue thì không") is None
    assert revised(written_for, "em dùng redis") == "transcript_shrank"
    assert revised("em dùng redis", "em dùng redis " + " ".join(["và"] * 13)) == "context_moved"
    assert revised("", "anything") is None


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
    assert cache.get("À, TTL theo key.") is None, "not a byte yet: nothing to replay"
    clip.append(b"\x01\x00" * 320)
    assert cache.get("À, TTL theo key.") is clip
    clip.finish(failed=True)
    assert cache.get("À, TTL theo key.") is None, "a failed synthesis is not replayed as silence"


def test_cancelled_or_failed_pcm_never_freezes_into_a_valid_clip() -> None:
    cache = SpeechAudioCache(voice_signature="v", directory=None)
    growing = GrowingClip(sample_rate=16000, channels=1)
    cache.put("line", growing, persist=False)
    growing.append(b"\x01\x00" * 320)
    assert growing.to_clip() is None, "unfinished"
    growing.finish(failed=True)
    assert growing.to_clip() is None, "failed: truncated audio is not a clip"
    assert cache.freeze("line", growing) is False and cache.get("line") is None
    ok = GrowingClip(sample_rate=16000, channels=1)
    cache.put("line", ok, persist=False)
    ok.append(b"\x02\x00" * 320)
    ok.finish()
    assert cache.freeze("line", ok) is True and isinstance(cache.get("line"), SpeechClip)


def test_a_stale_producer_cannot_overwrite_a_newer_cache_entry() -> None:
    cache = SpeechAudioCache(voice_signature="v", directory=None)
    old = GrowingClip(sample_rate=16000, channels=1)
    cache.put("line", old, persist=False)
    old.append(b"\x01\x00" * 320)
    new = GrowingClip(sample_rate=16000, channels=1)
    cache.put("line", new, persist=False)  # the same line prepared again
    new.append(b"\x02\x00" * 320)
    old.finish()
    assert cache.freeze("line", old) is False and cache.get("line") is new
    cache.forget("line", old)
    assert cache.get("line") is new, "forgetting the old producer leaves the new entry"


def test_pcm_pieces_are_sample_aligned_and_memory_is_bounded() -> None:
    clip = GrowingClip(sample_rate=16000, channels=1, max_bytes=1000)
    clip.append(b"\x01\x00\x02")  # an odd byte at the end is dropped, never shifts later samples
    assert clip.audio_ms == 0 and clip._bytes == 2
    clip.append(b"\x00" * 998)
    assert clip._bytes == 1000 and not clip.failed
    clip.append(b"\x00" * 2)       # over the bound: finished as failed, nothing more kept
    assert clip.failed and clip.finished and clip._bytes == 1000 and clip.to_clip() is None


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
    assert playout["complete"] is True and playout["audio_ms"] == 80 and playout["waits"] >= 1
    assert playout["wait_ms_total"] >= playout["wait_ms_max"] >= 0
    synthesized_event = events.of("voice_backchannel_synthesized")[0]
    assert synthesized_event["outcome"] == "complete" and synthesized_event["frozen"] is True


class _ForeverStream:
    """A synthesis that never ends (a hung provider), optionally after some PCM."""

    def __init__(self, log: list[str], *, pieces_before_hang: int = 0, closed: list[str] | None = None) -> None:
        self._n = pieces_before_hang
        self._closed = closed if closed is not None else []
        log.append("start")

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._n > 0:
            self._n -= 1
            return SimpleNamespace(frame=_Frame(b"\x01\x00" * 320))
        await asyncio.sleep(3600)
        raise StopAsyncIteration

    async def aclose(self) -> None:
        self._closed.append("closed")


@pytest.mark.asyncio
async def test_at_most_one_synthesis_per_session_and_the_previous_one_is_cancelled_and_awaited(monkeypatch) -> None:
    from src.modules.interviews.realtime import runtime as livekit_agent
    from tests.modules.voice.test_backchannel_runtime import _agent

    monkeypatch.setattr(livekit_agent, "PREPARE_FIRST_PCM_TIMEOUT_SECONDS", 1.0)
    agent, _events, _client, _spoken = _agent()
    agent._speech_cache = SpeechAudioCache(voice_signature="vieneu|test", directory=None)
    log: list[str] = []
    closed: list[str] = []
    agent._prepare_tts = SimpleNamespace(synthesize=lambda text: _ForeverStream(log, pieces_before_hang=1, closed=closed))
    assert await agent._prepare_backchannel_audio("Câu một.") is True
    first_task = agent._prepare_task
    assert first_task is not None and not first_task.done()
    assert await agent._prepare_backchannel_audio("Câu hai.") is True
    assert agent._prepare_task is not first_task and first_task.cancelled(), "the earlier synthesis was cancelled and awaited"
    assert closed == ["closed"], "its TTS stream was closed"
    assert agent._speech_cache.get("Câu một.") is None, "its truncated PCM was forgotten, not frozen"
    assert agent._prepare_closing == set()
    agent._cancel_prepare("test")
    await agent._drain_prepare()


@pytest.mark.asyncio
async def test_cancellation_before_and_after_the_first_pcm_never_leaves_a_valid_clip(monkeypatch) -> None:
    from src.modules.interviews.realtime import runtime as livekit_agent
    from tests.modules.voice.test_backchannel_runtime import _agent

    monkeypatch.setattr(livekit_agent, "PREPARE_FIRST_PCM_TIMEOUT_SECONDS", 0.05)
    agent, events, _client, _spoken = _agent()
    agent._speech_cache = SpeechAudioCache(voice_signature="vieneu|test", directory=None)
    log: list[str] = []
    # Before the first PCM: a hung provider -> bounded wait, cancelled, nothing cached.
    agent._prepare_tts = SimpleNamespace(synthesize=lambda text: _ForeverStream(log))
    assert await agent._prepare_backchannel_audio("Chưa có gì.") is False
    await agent._drain_prepare()
    assert agent._speech_cache.get("Chưa có gì.") is None and agent._prepare_task is None
    assert events.of("voice_backchannel_synthesized")[-1]["outcome"] == "cancelled"
    # After the first PCM: cancelled by the reply -> the partial clip is failed and forgotten.
    agent._prepare_tts = SimpleNamespace(synthesize=lambda text: _ForeverStream(log, pieces_before_hang=2))
    assert await agent._prepare_backchannel_audio("Có một chút.") is True
    growing = agent._speech_cache.get("Có một chút.")
    assert isinstance(growing, GrowingClip) and growing.has_pcm
    agent._cancel_prepare("reply_ready")
    await agent._drain_prepare()
    assert growing.failed and growing.to_clip() is None
    assert agent._speech_cache.get("Có một chút.") is None
    assert events.of("voice_backchannel_synthesized")[-1]["outcome"] == "cancelled"
    assert events.of("voice_backchannel_synthesized")[-1]["frozen"] is False


@pytest.mark.asyncio
async def test_shutdown_leaves_no_synthesis_or_inference_task_alive() -> None:
    from tests.modules.voice.test_backchannel_runtime import _agent

    agent, _events, _client, _spoken = _agent()
    agent._speech_cache = SpeechAudioCache(voice_signature="vieneu|test", directory=None)
    log: list[str] = []
    agent._prepare_tts = SimpleNamespace(synthesize=lambda text: _ForeverStream(log, pieces_before_hang=1))
    assert await agent._prepare_backchannel_audio("Một câu.") is True
    task = agent._prepare_task
    agent.on_user_speaking()
    agent._backchannel.observe("Em dùng Redis làm cache và đặt TTL năm phút cho từng key", phase="listening")
    inference = agent._backchannel._task
    await agent.shutdown_backchannel()
    assert task.done() and (inference is None or inference.done())
    assert agent._prepare_task is None and agent._prepare_closing == set()
    assert agent._backchannel._task is None and agent._backchannel._closing == set()
    assert agent._backchannel._pending is None
    assert agent._speech_cache.get("Một câu.") is None


@pytest.mark.asyncio
async def test_the_reply_and_a_superseding_line_cancel_a_running_synthesis() -> None:
    cancelled: list[str] = []
    prepared: list[str] = []

    async def prepare(text: str) -> bool:
        prepared.append(text)
        return True

    talker_lines = iter(["Câu một.", "Câu hai."])

    async def talker(request):
        line = next(talker_lines)
        yield {"type": "unit", "text": line, "kind": "contextual", "index": 1, "ttft_ms": 300, "at_ms": 400}
        yield {"type": "done", "kind": "contextual", "ttft_ms": 300, "total_ms": 500, "units": 1, "rejected": [], "reason": None}

    coordinator = bc.BackchannelCoordinator(
        talker, lambda text, kind: SimpleNamespace(done=lambda: False), enabled=True, min_gap_seconds=0.0,
        calls_per_turn=3, prepare=prepare, cancel_prepare=lambda reason: cancelled.append(reason),
    )
    coordinator.on_user_speaking()
    coordinator.observe("Em dùng Redis làm cache và đặt TTL năm phút cho từng key", phase="listening")
    await coordinator._task
    coordinator.observe("Em dùng Redis làm cache và đặt TTL năm phút cho từng key còn khi dữ liệu đổi thì bên ghi bắn event", phase="listening")
    await coordinator._task
    assert cancelled == ["superseded"], "the older line's synthesis was cancelled when replaced"
    coordinator.on_reply_ready()
    assert cancelled[1:] == ["reply_ready", "reply_ready"], "pending drop and the standalone cancel, both"


@pytest.mark.asyncio
async def test_speech_end_counts_usable_pcm_not_merely_available_pcm() -> None:
    async def talker(request):
        yield {"type": "unit", "text": "Ừ.", "kind": "minimal", "index": 1, "ttft_ms": 300, "at_ms": 400}
        yield {"type": "done", "kind": "minimal", "ttft_ms": 300, "total_ms": 500, "units": 1, "rejected": [], "reason": None}

    async def prepare(_text: str) -> bool:
        return True

    usable = {"value": True}
    events: list[tuple[str, dict]] = []
    played: list[str] = []
    coordinator = bc.BackchannelCoordinator(
        talker, lambda text, kind: played.append(text) or SimpleNamespace(done=lambda: False), enabled=True,
        min_gap_seconds=0.0, prepare=prepare, audio_usable=lambda text: usable["value"],
        on_event=lambda e, **f: events.append((e, f)),
    )
    coordinator.on_user_speaking()
    coordinator.observe("Em dùng Redis làm cache và đặt TTL năm phút cho từng key", phase="listening")
    await coordinator._task
    usable["value"] = False  # the synthesis failed after the first PCM
    coordinator.on_user_stopped()
    speech_end = [f for e, f in events if e == "backchannel_speech_end"][0]
    assert speech_end == {"pcm_available": True, "pcm_usable": False, "pending": True, "rejection": None}
    assert coordinator.stats.speech_ends_with_audio == 0
    assert played == ["Ừ."], "still spoken -- synthesized at playback"
    assert [f for e, f in events if e == "backchannel_spoken"][0]["prepared_audio"] is False


@pytest.mark.asyncio
async def test_pcm_usable_is_the_playback_eligibility_check_itself() -> None:
    """Run-7 item 1: a pending line with valid PCM whose transcript was revised,
    whose turn ended, or whose reply exists is not usable -- and not played."""
    async def talker(request):
        yield {"type": "unit", "text": "Ừ.", "kind": "minimal", "index": 1, "ttft_ms": 300, "at_ms": 400}
        yield {"type": "done", "kind": "minimal", "ttft_ms": 300, "total_ms": 500, "units": 0, "rejected": [], "reason": None}

    async def prepare(_text: str) -> bool:
        return True

    for case in ("revised", "new_turn", "reply_ready"):
        events: list[tuple[str, dict]] = []
        played: list[str] = []
        coordinator = bc.BackchannelCoordinator(
            talker, lambda text, kind: played.append(text) or SimpleNamespace(done=lambda: False), enabled=True,
            min_gap_seconds=0.0, prepare=prepare, audio_usable=lambda text: True,
            on_event=lambda e, **f: events.append((e, f)),
        )
        coordinator.on_user_speaking()
        written_for = "em dùng redis làm cache và đặt ttl năm phút cho từng key"
        coordinator.observe(written_for, phase="listening")
        await coordinator._task
        if case == "revised":
            coordinator.note_transcript("em dùng kafka làm cache và đặt ttl năm phút cho từng key")
        elif case == "new_turn":
            coordinator._generation += 1
        else:
            coordinator._reply_ready = True
        coordinator.on_user_stopped()
        end = [f for e, f in events if e == "backchannel_speech_end"][0]
        assert end["pcm_available"] is True and end["pcm_usable"] is False, case
        assert end["rejection"] in ("transcript_revised", "new_turn", "reply_ready"), case
        assert played == [], case
        assert coordinator.stats.speech_ends_with_audio == 0, case


@pytest.mark.asyncio
async def test_a_stream_that_stalls_after_the_first_pcm_is_ended_and_playback_finishes(monkeypatch) -> None:
    """Run-7 item 2: VieNeu sets no read timeout; a stall after audio began must
    not leave a playing clip waiting forever."""
    from src.modules.interviews.realtime import runtime as livekit_agent
    from tests.modules.voice.test_backchannel_runtime import _agent

    monkeypatch.setattr(livekit_agent, "PREPARE_IDLE_TIMEOUT_SECONDS", 0.05)
    agent, events, _client, _spoken = _agent()
    agent._speech_cache = SpeechAudioCache(voice_signature="vieneu|test", directory=None)
    log: list[str] = []
    closed: list[str] = []
    agent._prepare_tts = SimpleNamespace(synthesize=lambda text: _ForeverStream(log, pieces_before_hang=2, closed=closed))
    assert await agent._prepare_backchannel_audio("Bị kẹt giữa chừng.") is True
    clip = agent._speech_cache.get("Bị kẹt giữa chừng.")
    assert isinstance(clip, GrowingClip)

    async def text_stream():
        yield "Bị kẹt giữa chừng."

    frames = await asyncio.wait_for(_collect(agent.tts_node(text_stream(), None)), 2.0)
    assert len(frames) == 2, "played what had arrived, then ended instead of waiting"
    await agent._prepare_task
    assert clip.failed and clip.to_clip() is None and closed == ["closed"]
    assert events.of("voice_backchannel_synthesized")[-1]["outcome"] == "stalled"
    assert agent._speech_cache.get("Bị kẹt giữa chừng.") is None, "a stalled clip is not replayed"
    playout = events.of("voice_backchannel_playout")[-1]
    assert playout["complete"] is False and playout["waits"] >= 1


@pytest.mark.asyncio
async def test_a_consumer_whose_producer_vanished_gives_up_after_the_bound(monkeypatch) -> None:
    monkeypatch.setattr(GrowingClip, "STREAM_WAIT_TIMEOUT_SECONDS", 0.05)
    clip = GrowingClip(sample_rate=16000, channels=1)
    clip.append(b"\x01\x00" * 320)
    pieces = await asyncio.wait_for(_collect(clip.stream()), 1.0)
    assert len(pieces) == 1 and clip.failed and clip.finished


@pytest.mark.asyncio
async def test_shutdown_refuses_new_preparation_while_draining(monkeypatch) -> None:
    """Run-7 item 3: a deliver that races the shutdown starts no synthesis and no inference."""
    from tests.modules.voice.test_backchannel_runtime import _agent

    agent, _events, client, _spoken = _agent()
    agent._speech_cache = SpeechAudioCache(voice_signature="vieneu|test", directory=None)
    log: list[str] = []
    agent._prepare_tts = SimpleNamespace(synthesize=lambda text: _ForeverStream(log, pieces_before_hang=1))
    assert await agent._prepare_backchannel_audio("Một.") is True
    first = agent._prepare_task

    async def race() -> None:
        # Lands while shutdown is awaiting the drain of `first`.
        await asyncio.sleep(0)
        assert agent._backchannel.observe("Em dùng Redis làm cache và đặt TTL năm phút cho từng key", phase="listening") is False
        assert await agent._prepare_backchannel_audio("Hai.") is False

    await asyncio.gather(agent.shutdown_backchannel(), race())
    assert first.done() and agent._prepare_task is None and agent._prepare_closing == set()
    assert log == ["start"], "no second synthesis was ever started"
    assert agent._backchannel._task is None and agent._backchannel.enabled is False
    assert client.streams == []


async def _collect(aiter):
    return [x async for x in aiter]


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
