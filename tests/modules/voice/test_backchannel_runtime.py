"""The voice agent with backchanneling on: when it asks, what it stops saying, what wins.

The agent is built bare (no room); the API is a scripted NDJSON stream.
"""

from __future__ import annotations

import asyncio
import json
from time import perf_counter
from types import SimpleNamespace

import pytest

from src.modules.interviews.realtime import runtime as livekit_agent
from src.modules.interviews.realtime.anticipation import AnticipationEngine
from src.modules.interviews.realtime.backchannel import BackchannelCoordinator
from src.modules.interviews.realtime.endpointing import AnswerWindow
from src.modules.interviews.realtime.events import VOICE_EVENTS


class _Events:
    def __init__(self) -> None:
        self.items: list[tuple[str, dict]] = []

    def emit(self, event: str, **fields) -> None:
        self.items.append((event, fields))

    def state(self, _state: str, **_fields) -> None:
        pass

    def of(self, name: str) -> list[dict]:
        return [f for e, f in self.items if e == name]


class _Participant:
    async def publish_data(self, payload: str, **_kwargs) -> None:
        json.loads(payload)


class _Body:
    def __init__(self, body: dict) -> None:
        self._body = body

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return self._body


class _Stream:
    def __init__(self, lines: list[dict], *, delay: float = 0.0) -> None:
        self.lines, self.delay = lines, delay

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def raise_for_status(self) -> None:
        pass

    async def aiter_lines(self):
        for line in self.lines:
            if self.delay:
                await asyncio.sleep(self.delay)
            yield json.dumps(line, ensure_ascii=False)


class _RuntimeClient:
    def __init__(self, *, core_delay: float = 0.0, backchannel_delay: float = 0.0) -> None:
        self.posts: list[tuple[str, dict]] = []
        self.streams: list[tuple[str, dict]] = []
        self.core_delay = core_delay
        self.backchannel_delay = backchannel_delay

    async def post(self, path: str, *, json: dict) -> _Body:
        self.posts.append((path, json))
        if path.endswith("/prefetch"):
            return _Body({"warmed": [], "activeTurn": True, "ms": 1})
        await asyncio.sleep(self.core_delay)
        return _Body({"action": "PROBE", "sessionStatus": "OPEN", "assistantResponse": {"content": "Khi Redis sập thì sao?"}})

    def stream(self, method: str, path: str, *, json: dict):
        self.streams.append((path, json))
        return _Stream([
            {"type": "unit", "text": "À, TTL theo từng key.", "kind": "reflective", "index": 1, "ttft_ms": 300, "at_ms": 400},
            {"type": "done", "kind": "reflective", "ttft_ms": 300, "total_ms": 450, "units": 1, "rejected": [], "reason": None},
        ], delay=self.backchannel_delay)


class _Handle:
    def __init__(self, text: str, kind: str | None) -> None:
        self.text, self.kind, self.playing, self.interrupted = text, kind, True, False

    def done(self) -> bool:
        return not self.playing

    def interrupt(self):
        self.interrupted, self.playing = True, False
        return self

    async def wait_for_playout(self) -> None:
        pass


def _agent(*, enabled: bool = True, core_delay: float = 0.0, backchannel_delay: float = 0.0):
    agent = livekit_agent.VoiceLabAgent.__new__(livekit_agent.VoiceLabAgent)
    events, client = _Events(), _RuntimeClient(core_delay=core_delay, backchannel_delay=backchannel_delay)
    agent._room = SimpleNamespace(local_participant=_Participant())
    agent._events = events
    agent._runtime_client = client
    agent._session_id = "session-1"
    agent._room_name = "room-1"
    agent._locale = "vi-VN"
    agent._stt_provider = "qwen"
    agent._enable_speculative_preview = False
    agent._answer = AnswerWindow()
    agent._answer_pause_nudged = False
    agent._answer_pause_nudge_task = None
    agent._reprompt_task = None
    agent._commit_task = None
    agent._turn_generation = 0
    agent._hybrid_enabled = False
    agent._anticipation = AnticipationEngine(lambda _t, _k: None, enabled=False)
    agent._core_in_flight = False
    agent._repeat_after_core = False
    agent._last_answer_metrics = {}
    agent._last_interruption_telemetry = {}
    agent._last_interview_speech_text = "Bạn xử lý cache invalidation thế nào?"
    agent._held_speech_text = ""
    agent._held_resume_text = ""
    agent._active_speech_handle = None
    agent._active_speech_text = ""
    agent._active_speech_kind = "question"
    agent._speech_ended_at = None
    agent._submitted_at = None
    agent._core_completed_at = None
    agent._warm_tts = None
    spoken: list[_Handle] = []

    def speak(text: str, **kwargs):
        handle = _Handle(text, kwargs.get("kind"))
        spoken.append(handle)
        return handle

    agent.speak = speak
    agent._backchannel = BackchannelCoordinator(
        agent._backchannel_infer, agent._speak_backchannel, enabled=enabled, min_gap_seconds=0.0, hold_seconds=0.2,
        on_event=lambda event, **fields: agent.report(f"voice_{event}", **fields),
    )
    return agent, events, client, spoken


def _message(text: str) -> SimpleNamespace:
    return SimpleNamespace(text_content=text, transcript_confidence=None, metrics={}, id="m-1", content=[text])


def test_the_events_are_in_the_vocabulary() -> None:
    assert {"voice_backchannel", "voice_backchannel_spoken", "voice_backchannel_discarded", "voice_backchannel_overlap"} <= VOICE_EVENTS


@pytest.mark.asyncio
async def test_a_finished_fragment_asks_the_talker_and_the_unit_is_spoken_not_a_nudge(monkeypatch) -> None:
    monkeypatch.setattr(livekit_agent, "processing_filler_enabled", lambda: False)
    monkeypatch.delenv("VOICE_ANSWER_PAUSE_NUDGE_ENABLED", raising=False)
    agent, events, client, spoken = _agent()
    agent.on_user_stopped()
    await agent.on_user_turn_completed(None, _message("Em dùng Redis làm cache và đặt TTL năm phút cho từng key."))
    assert agent._answer_pause_nudge_task is None, "the template nudge is not used with backchanneling"
    await agent._backchannel._task
    assert [p for p, _ in client.streams] == ["/ai/voice-lab/livekit/runtime/backchannel"]
    request = client.streams[0][1]
    assert request["phase"] == "listening" and request["transcript"].startswith("Em dùng Redis") and request["signals"]["fragments"] == 1
    assert [h.kind for h in spoken] == ["backchannel"] and spoken[0].text == "À, TTL theo từng key."
    assert agent._filler_handle is spoken[0], "the reply can cut it like any filler"
    assert events.of("voice_backchannel_spoken")[0]["kind"] == "reflective"
    agent._commit_task.cancel()


@pytest.mark.asyncio
async def test_a_stable_partial_lets_the_talker_prepare_while_the_candidate_speaks(monkeypatch) -> None:
    """Audit items 1-2: generation during speech, playback at the pause."""
    monkeypatch.setattr(livekit_agent, "processing_filler_enabled", lambda: False)
    agent, events, client, spoken = _agent()
    agent._enable_speculative_preview = False
    agent.on_user_speaking()
    partial = "Em dùng Redis làm cache và đặt TTL năm phút cho từng key còn khi"
    agent.on_interim_transcript(partial)   # first sighting: not yet stable
    assert client.streams == []
    agent.on_interim_transcript(partial)   # re-emitted unchanged: stable
    await asyncio.sleep(0.01)              # the coordinator's task reaches the API
    assert len(client.streams) == 1 and client.streams[0][1]["speaking"] is True
    await asyncio.sleep(0.05)
    assert spoken == [], "nothing plays while the candidate is talking"
    agent.on_user_stopped()
    await agent._backchannel._task
    assert [h.kind for h in spoken] == ["backchannel"]


@pytest.mark.asyncio
async def test_prepared_audio_goes_into_the_speech_cache_and_playback_is_a_cache_hit(monkeypatch) -> None:
    """The line is synthesized with the same adapter and voice while the
    candidate speaks; speak() later replays it through tts_node."""
    from src.modules.interviews.realtime.tts.cache import SpeechAudioCache

    agent, _events, _client, _spoken = _agent()
    agent._speech_cache = SpeechAudioCache(voice_signature="vieneu|test", directory=None)
    synthesized: list[str] = []

    class _Frame:
        def __init__(self, data: bytes) -> None:
            self.data, self.sample_rate, self.num_channels, self.samples_per_channel = data, 16000, 1, len(data) // 2

    class _Stream:
        def __init__(self, text: str) -> None:
            synthesized.append(text)
            self._frames = iter([SimpleNamespace(frame=_Frame(b"\x01\x00" * 320)), SimpleNamespace(frame=_Frame(b"\x02\x00" * 320))])

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self._frames)
            except StopIteration:
                raise StopAsyncIteration

        async def aclose(self) -> None:
            pass

    agent._prepare_tts = SimpleNamespace(synthesize=lambda text: _Stream(text))
    assert await agent._prepare_backchannel_audio("À, TTL theo từng key.") is True
    assert synthesized == ["À, TTL theo từng key."]
    clip = agent._speech_cache.get("À, TTL theo từng key.")
    assert clip is not None and len(clip.pcm) == 1280 and clip.sample_rate == 16000
    # Preparing the same line again costs nothing.
    assert await agent._prepare_backchannel_audio("À, TTL theo từng key.") is True and len(synthesized) == 1

    # Playback: tts_node finds the clip and never calls the TTS.
    async def text_stream():
        yield "À, TTL theo từng key."

    frames = [f async for f in agent.tts_node(text_stream(), None)]
    assert len(frames) >= 2 and synthesized == ["À, TTL theo từng key."]


@pytest.mark.asyncio
async def test_the_tts_connection_is_kept_warm_from_the_first_word_when_backchanneling(monkeypatch) -> None:
    monkeypatch.setattr(livekit_agent, "TTS_KEEPALIVE_SECONDS", 0.01)
    pings: list[int] = []

    async def warm_tts() -> bool:
        pings.append(1)
        return True

    agent, *_ = _agent()
    agent._warm_tts = warm_tts
    agent.on_user_speaking()
    await asyncio.sleep(0.05)
    assert len(pings) >= 2
    agent._tts_keepalive_task.cancel()
    off, *_ = _agent(enabled=False)
    off._warm_tts = warm_tts
    off.on_user_speaking()
    assert getattr(off, "_tts_keepalive_task", None) is None


@pytest.mark.asyncio
async def test_without_a_tts_or_cache_nothing_is_prepared_and_playback_synthesizes() -> None:
    agent, *_ = _agent()
    agent._speech_cache = None
    assert await agent._prepare_backchannel_audio("À, TTL theo từng key.") is False


@pytest.mark.asyncio
async def test_the_submit_asks_for_a_processing_acknowledgment_and_the_reply_cuts_it(monkeypatch) -> None:
    monkeypatch.setattr(livekit_agent, "processing_filler_enabled", lambda: True)  # a template: must not run
    agent, events, client, spoken = _agent(core_delay=0.05)
    agent._answer.add("Em dùng Redis làm cache và đặt TTL năm phút cho từng key.", message_id="m1")
    await agent._commit_answer()
    processing = [j for p, j in client.streams if j["phase"] == "processing"]
    assert len(processing) == 1 and processing[0]["signals"]["fragments"] == 1
    kinds = [h.kind for h in spoken]
    assert "filler" not in kinds, "the processing filler template is not spoken with backchanneling"
    assert kinds[-1] == "question"
    backchannel = next(h for h in spoken if h.kind == "backchannel")
    assert backchannel.interrupted, "the authoritative reply cut the backchannel still playing"
    assert events.of("voice_filler_interrupted") == [{}]


@pytest.mark.asyncio
async def test_a_backchannel_still_generating_when_core_answers_is_discarded(monkeypatch) -> None:
    monkeypatch.setattr(livekit_agent, "processing_filler_enabled", lambda: False)
    agent, events, client, spoken = _agent(core_delay=0.0, backchannel_delay=0.2)
    agent._answer.add("Em dùng Redis làm cache và đặt TTL năm phút cho từng key.", message_id="m1")
    await agent._commit_answer()
    await asyncio.sleep(0.05)
    assert [h.kind for h in spoken] == ["question"], "nothing stale was spoken after the reply"
    assert agent._backchannel.stats.reasons.get("cancelled:reply_ready") == 1


@pytest.mark.asyncio
async def test_a_new_answer_resets_the_turn_and_a_core_failure_also_ends_the_backchannel(monkeypatch) -> None:
    monkeypatch.setattr(livekit_agent, "processing_filler_enabled", lambda: False)
    agent, events, client, spoken = _agent(backchannel_delay=0.2)
    agent._answer.add("Em dùng Redis.", message_id="m1")
    agent._backchannel.observe("Em dùng Redis.", phase="listening")
    agent._answer.take()
    agent.on_user_speaking()  # a new answer starts
    assert agent._backchannel.stats.reasons.get("cancelled:new_turn") == 1

    async def broken(path, *, json):
        raise RuntimeError("core down")

    client.post = broken  # type: ignore[assignment]
    agent._answer.add("Em dùng Redis làm cache và đặt TTL năm phút.", message_id="m2")
    await agent._commit_answer()
    await asyncio.sleep(0.01)
    assert agent._backchannel.stats.reasons.get("cancelled:reply_ready") == 1
    assert [h.kind for h in spoken][-1] is None  # the core-error line, not a backchannel


@pytest.mark.asyncio
async def test_disabled_backchanneling_leaves_the_nudge_path_alone(monkeypatch) -> None:
    monkeypatch.delenv("VOICE_ANSWER_PAUSE_NUDGE_ENABLED", raising=False)
    agent, _, client, _ = _agent(enabled=False)
    agent._answer.add("Em đang nghĩ", message_id="m1")
    agent._schedule_answer_pause_nudge()
    assert agent._answer_pause_nudge_task is not None
    agent._answer_pause_nudge_task.cancel()
    assert not agent._backchannel.observe("Em đang nghĩ", phase="listening") and client.streams == []
