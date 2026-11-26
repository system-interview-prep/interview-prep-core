"""The endpointing chain after the critical-path profile (2026-11-26).

What the stored pilot events showed, per turn: VAD end of speech → Qwen final
0.27 s → LiveKit fixed delay 0.80 s → answer window 1.80 s = the 2.87 s
"speech end → submit" the pilot reported. These tests pin the three changes
made from that: no fixed LiveKit delay after the final (test_recognition.py),
a commit wait that can be anchored on speech end, and a reply that never
waits behind a nudge still playing -- plus the telemetry that makes the chain
visible on every submit, and the deterministic prefetch that costs no call.
"""

from __future__ import annotations

import asyncio
import json
from time import perf_counter
from types import SimpleNamespace

import pytest

from src.modules.interviews.realtime import endpointing
from src.modules.interviews.realtime import runtime as livekit_agent
from src.modules.interviews.realtime.anticipation import AnticipationEngine
from src.modules.interviews.realtime.endpointing import AnswerWindow
from src.modules.interviews.realtime.events import VOICE_EVENTS

# ----------------------------------------------------------------- pure parts


def test_the_transcript_anchor_waits_the_full_window_from_now() -> None:
    now = 100.0
    assert endpointing.remaining_commit_delay(1.8, speech_ended_at=now - 1.5, now=now, anchor="transcript") == 1.8
    assert endpointing.remaining_commit_delay(1.8, speech_ended_at=None, now=now, anchor="speech_end") == 1.8


def test_the_speech_end_anchor_does_not_wait_again_for_what_the_recognizer_spent() -> None:
    now = 100.0
    # The final arrived 1.05 s after VAD end (0.27 s Qwen + 0.8 s LiveKit, as measured): 0.75 s left.
    assert endpointing.remaining_commit_delay(1.8, speech_ended_at=now - 1.05, now=now, anchor="speech_end") == pytest.approx(0.75)
    # A human speaker's final can arrive after the whole window: commit now, never a negative wait.
    assert endpointing.remaining_commit_delay(1.8, speech_ended_at=now - 2.5, now=now, anchor="speech_end") == 0.0


def test_the_anchor_defaults_to_the_transcript_until_the_human_benchmark_says_otherwise(monkeypatch) -> None:
    monkeypatch.delenv("VOICE_ANSWER_COMMIT_ANCHOR", raising=False)
    assert endpointing.commit_anchor() == "transcript"
    monkeypatch.setenv("VOICE_ANSWER_COMMIT_ANCHOR", "speech_end")
    assert endpointing.commit_anchor() == "speech_end"
    monkeypatch.setenv("VOICE_ANSWER_COMMIT_ANCHOR", "nonsense")
    assert endpointing.commit_anchor() == "transcript"


def test_the_nominal_windows_are_untouched(monkeypatch) -> None:
    """The brief: do not reduce silence thresholds blindly. The constants did not move."""
    monkeypatch.delenv("VOICE_ANSWER_COMMIT_SILENCE", raising=False)
    monkeypatch.delenv("VOICE_ANSWER_SHORT_COMMIT_SILENCE", raising=False)
    window = AnswerWindow()
    assert (window.commit_silence, window.short_commit_silence) == (1.8, 2.2)


def test_the_pause_nudge_is_off_by_default_and_can_be_turned_on(monkeypatch) -> None:
    """Off since 26/11: it said "bạn cứ nói tiếp nhé" at the end of every answer."""
    monkeypatch.delenv("VOICE_ANSWER_PAUSE_NUDGE_ENABLED", raising=False)
    assert not endpointing.answer_pause_nudge_enabled()
    monkeypatch.setenv("VOICE_ANSWER_PAUSE_NUDGE_ENABLED", "1")
    assert endpointing.answer_pause_nudge_enabled()


def test_the_new_events_are_accepted_by_the_reporter() -> None:
    assert {"voice_endpointing", "voice_filler_interrupted", "voice_prefetch"} <= VOICE_EVENTS


# ------------------------------------------------------------------ the agent


class _Events:
    def __init__(self) -> None:
        self.items: list[tuple[str, dict]] = []

    def emit(self, event: str, **fields) -> None:
        self.items.append((event, fields))

    def state(self, _state: str, **_fields) -> None:
        pass

    def of(self, name: str) -> list[dict]:
        return [fields for event, fields in self.items if event == name]


class _Participant:
    async def publish_data(self, payload: str, **_kwargs) -> None:
        json.loads(payload)


class _Response:
    def __init__(self, body: dict) -> None:
        self._body = body

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return self._body


class _RuntimeClient:
    def __init__(self) -> None:
        self.posts: list[tuple[str, dict]] = []

    async def post(self, path: str, *, json: dict) -> _Response:
        self.posts.append((path, json))
        if path.endswith("/prefetch"):
            return _Response({"warmed": ["candidate_memory", "playbook"], "activeTurn": True, "ms": 7})
        return _Response({"action": "NEXT_QUESTION", "sessionStatus": "OPEN",
                          "assistantResponse": {"content": "Câu hỏi tiếp theo là gì?"}})


class _Handle:
    def __init__(self, *, playing: bool) -> None:
        self.playing = playing
        self.interrupted = False

    def done(self) -> bool:
        return not self.playing

    def interrupt(self):
        self.interrupted = True
        self.playing = False
        return self

    async def wait_for_playout(self) -> None:
        pass


def _agent(*, warm_tts=None) -> tuple[livekit_agent.VoiceLabAgent, _Events, _RuntimeClient, list[_Handle]]:
    agent = livekit_agent.VoiceLabAgent.__new__(livekit_agent.VoiceLabAgent)
    events, client = _Events(), _RuntimeClient()
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
    agent._last_interview_speech_text = "Câu hỏi vừa rồi là gì?"
    agent._held_speech_text = ""
    agent._held_resume_text = ""
    agent._active_speech_handle = None
    agent._active_speech_text = ""
    agent._active_speech_kind = "question"
    agent._speech_ended_at = None
    agent._submitted_at = None
    agent._core_completed_at = None
    agent._warm_tts = warm_tts
    spoken: list[_Handle] = []

    def speak(text: str, **kwargs):
        handle = _Handle(playing=True)
        handle.text, handle.kind = text, kwargs.get("kind")
        spoken.append(handle)
        return handle

    agent.speak = speak
    return agent, events, client, spoken


@pytest.mark.asyncio
async def test_the_commit_wait_is_measured_from_speech_end_when_anchored(monkeypatch) -> None:
    monkeypatch.setenv("VOICE_ANSWER_COMMIT_ANCHOR", "speech_end")
    agent, *_ = _agent()
    agent._speech_ended_at = perf_counter() - 1.0  # VAD ended a second ago; the final has just arrived
    agent._schedule_commit(1.8)
    try:
        assert agent._commit_delay_s == 1.8
        assert 0.7 <= agent._commit_effective_s <= 0.8
    finally:
        agent._commit_task.cancel()
    monkeypatch.setenv("VOICE_ANSWER_COMMIT_ANCHOR", "transcript")
    agent._schedule_commit(1.8)
    try:
        assert agent._commit_effective_s == 1.8
    finally:
        agent._commit_task.cancel()


@pytest.mark.asyncio
async def test_the_submit_reports_the_whole_chain_and_the_reply_cuts_a_nudge_still_playing(monkeypatch) -> None:
    monkeypatch.setattr(livekit_agent, "processing_filler_enabled", lambda: False)
    monkeypatch.delenv("VOICE_ANSWER_COMMIT_ANCHOR", raising=False)
    agent, events, client, spoken = _agent()
    now = perf_counter()
    agent._speech_ended_at = now - 2.9
    agent._last_final_at = now - 2.6
    agent._turn_completed_at = now - 1.8
    agent._commit_delay_s, agent._commit_effective_s = 1.8, 1.8
    agent._answer.add("Em dùng Redis làm cache và đặt TTL năm phút.", message_id="m1")
    nudge = _Handle(playing=True)
    agent._filler_handle = nudge
    agent._answer_pause_nudged = True

    await agent._commit_answer()

    chain = events.of("voice_endpointing")
    assert len(chain) == 1
    chain = chain[0]
    assert chain["anchor"] == "transcript" and chain["commit_delay_ms"] == 1800 and chain["effective_delay_ms"] == 1800
    assert 250 <= chain["vad_to_final_ms"] <= 350
    assert 750 <= chain["final_to_turn_ms"] <= 850
    assert 1750 <= chain["turn_to_submit_ms"] <= 1900
    assert chain["fragments"] == 1 and chain["nudged"] is True
    # The question did not queue behind the nudge.
    assert nudge.interrupted and events.of("voice_filler_interrupted") == [{}]
    assert spoken[-1].kind == "question"
    assert agent._filler_handle is None


@pytest.mark.asyncio
async def test_a_nudge_that_already_finished_is_left_alone(monkeypatch) -> None:
    monkeypatch.setattr(livekit_agent, "processing_filler_enabled", lambda: False)
    agent, events, _client, _spoken = _agent()
    agent._answer.add("Em dùng Redis làm cache và đặt TTL năm phút.", message_id="m1")
    finished = _Handle(playing=False)
    agent._filler_handle = finished
    await agent._commit_answer()
    assert not finished.interrupted and events.of("voice_filler_interrupted") == []


@pytest.mark.asyncio
async def test_the_nudge_is_not_spoken_when_disabled(monkeypatch) -> None:
    monkeypatch.setenv("VOICE_ANSWER_PAUSE_NUDGE_ENABLED", "0")
    agent, *_ = _agent()
    agent._answer.add("Em đang nghĩ", message_id="m1")
    agent._schedule_answer_pause_nudge()
    assert agent._answer_pause_nudge_task is None


def test_the_nudge_lines_are_fixed_lines_whose_audio_is_cached() -> None:
    """In the pilot every nudge was synthesized (1.1 s to first byte): it was not a fixed line."""
    agent, *_ = _agent()
    assert set(endpointing.answer_pause_nudges("vi-VN")) <= agent._fixed_lines()


@pytest.mark.asyncio
async def test_a_new_answer_starts_one_prefetch_that_costs_no_call_and_warms_the_tts() -> None:
    warmed: list[str] = []

    async def warm_tts() -> bool:
        warmed.append("tts")
        return True

    agent, events, client, _spoken = _agent(warm_tts=warm_tts)
    agent.on_user_speaking()  # the candidate starts an answer
    agent.on_user_speaking()  # ...and keeps talking (same answer): no second prefetch
    await asyncio.sleep(0)
    await agent._prefetch_task
    assert [p for p, _ in client.posts] == ["/ai/voice-lab/livekit/runtime/prefetch"]
    report = events.of("voice_prefetch")
    assert len(report) == 1 and report[0]["ok"] is True and report[0]["warmed"] == "candidate_memory,playbook"
    assert warmed == ["tts"]
    # The next answer prefetches again.
    agent._answer.add("một câu", message_id="m1")
    agent._answer.take()
    agent.on_user_speaking()
    await agent._prefetch_task
    assert len(client.posts) == 2


@pytest.mark.asyncio
async def test_a_failed_prefetch_is_reported_and_costs_the_turn_nothing() -> None:
    agent, events, client, _spoken = _agent()

    async def broken(path: str, *, json: dict):
        raise RuntimeError("api down")

    client.post = broken  # type: ignore[assignment]
    agent.on_user_speaking()
    await agent._prefetch_task
    assert events.of("voice_prefetch")[0]["ok"] is False
    assert events.of("voice_prefetch")[0]["error_type"] == "RuntimeError"


@pytest.mark.asyncio
async def test_the_tts_connection_is_kept_warm_while_core_thinks(monkeypatch) -> None:
    """VieNeu's first byte is 0.45 s on a warm connection and 1.1 s after a few
    seconds idle (measured 2026-11-26): the reply must not pay the cold start."""
    monkeypatch.setattr(livekit_agent, "processing_filler_enabled", lambda: False)
    monkeypatch.setattr(livekit_agent, "TTS_KEEPALIVE_SECONDS", 0.01)
    pings: list[float] = []

    async def warm_tts() -> bool:
        pings.append(perf_counter())
        return True

    agent, _events, client, _spoken = _agent(warm_tts=warm_tts)

    async def slow_core(path: str, *, json: dict):
        await asyncio.sleep(0.05)
        return _Response({"action": "NEXT_QUESTION", "sessionStatus": "OPEN",
                          "assistantResponse": {"content": "Tiếp theo?"}})

    client.post = slow_core  # type: ignore[assignment]
    agent._answer.add("Em dùng Redis làm cache và đặt TTL năm phút.", message_id="m1")
    await agent._commit_answer()
    assert len(pings) >= 2, "pinged at submit and again while Core was still working"
    await asyncio.sleep(0)  # let the cancellation land
    assert agent._tts_keepalive_task.cancelled()
