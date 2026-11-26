"""§13 instrumentation: the runtime marks the turn clock; the report reads it.

No room, no provider: the agent is built bare, the events are synthetic.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.modules.interviews.realtime import runtime as livekit_agent
from src.modules.interviews.realtime.anticipation import AnticipationEngine
from src.modules.interviews.realtime.endpointing import AnswerWindow
from src.modules.interviews.realtime.events import VOICE_EVENTS

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "eval" / "interviews" / "voice" / "livekit_turn_metrics.py"
_spec = importlib.util.spec_from_file_location("livekit_turn_metrics", _SCRIPT)
metrics = importlib.util.module_from_spec(_spec)
sys.modules["livekit_turn_metrics"] = metrics
_spec.loader.exec_module(metrics)


# ------------------------------------------------------------ the runtime


class _Events:
    def __init__(self) -> None:
        self.items: list[tuple[str, dict]] = []

    def emit(self, event: str, **fields) -> None:
        self.items.append((event, fields))

    def state(self, _state: str, **_fields) -> None:
        pass


class _Response:
    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return {"action": "NEXT_QUESTION", "sessionStatus": "OPEN", "assistantResponse": {"content": "Câu tiếp theo?"}}


class _Client:
    async def post(self, _path: str, *, json: dict) -> _Response:
        await asyncio.sleep(0.01)
        return _Response()


def _agent() -> livekit_agent.VoiceLabAgent:
    agent = livekit_agent.VoiceLabAgent.__new__(livekit_agent.VoiceLabAgent)
    agent._room = SimpleNamespace(local_participant=SimpleNamespace(publish_data=_noop))
    agent._events = _Events()
    agent._runtime_client = _Client()
    agent._session_id = "session-1"
    agent._room_name = "room-1"
    agent._locale = "vi-VN"
    agent._stt_provider = "qwen"
    agent._enable_speculative_preview = False
    agent._answer = AnswerWindow()
    agent._answer.add("Em dùng Redis làm cache và đặt TTL năm phút.", message_id="a-1")
    agent._answer_pause_nudged = False
    agent._answer_pause_nudge_task = None
    agent._reprompt_task = None
    agent._commit_task = None
    agent._turn_generation = 0
    agent._hybrid_enabled = False
    agent._anticipation = AnticipationEngine(lambda _t, _k: None, enabled=False)
    agent._core_in_flight = False
    agent._repeat_after_core = False
    agent._last_answer_metrics = {"transcription_delay": 0.42, "end_of_turn_delay": 0.6}
    agent._last_interruption_telemetry = {}
    agent._last_interview_speech_text = "Bạn xử lý cache thế nào?"
    agent._held_speech_text = ""
    agent._held_resume_text = ""
    agent._active_speech_handle = None
    agent._active_speech_text = ""
    agent._active_speech_kind = "question"
    agent.speak = lambda text, **kw: SimpleNamespace(wait_for_playout=lambda: None)
    return agent


async def _noop(*_a, **_k) -> None:
    pass


def _events_of(agent, name: str) -> list[dict]:
    return [fields for event, fields in agent._events.items if event == name]


@pytest.mark.asyncio
async def test_the_runtime_marks_speech_end_submit_and_reply_start(monkeypatch) -> None:
    monkeypatch.setattr(livekit_agent, "processing_filler_enabled", lambda: False)
    agent = _agent()

    agent.on_user_stopped()                      # speech end
    await agent._commit_answer()                 # submit → Core → answer
    agent._active_speech_kind = "question"
    agent.on_agent_speaking()                    # the reply starts playing

    submitted = _events_of(agent, "voice_answer_submitted")[0]
    assert submitted["since_speech_end_ms"] is not None and submitted["since_speech_end_ms"] >= 0
    assert submitted["transcription_delay_ms"] == 420
    assert submitted["end_of_turn_delay_ms"] == 600

    started = _events_of(agent, "voice_reply_started")
    assert len(started) == 1 and started[0]["kind"] == "question"
    assert started[0]["since_speech_end_ms"] >= submitted["since_speech_end_ms"]
    assert started[0]["since_submit_ms"] is not None and started[0]["since_submit_ms"] >= 0
    assert started[0]["since_core_ms"] is not None and started[0]["since_core_ms"] >= 0


@pytest.mark.asyncio
async def test_an_utterance_before_any_answer_reports_no_elapsed_times() -> None:
    """The opening question has no speech end, submit or Core behind it."""
    agent = _agent()
    agent.on_agent_speaking()
    started = _events_of(agent, "voice_reply_started")[0]
    assert started == {"kind": "question", "since_speech_end_ms": None, "since_submit_ms": None, "since_core_ms": None}


def test_the_new_event_is_accepted_by_the_api_vocabulary() -> None:
    assert "voice_reply_started" in VOICE_EVENTS


# ------------------------------------------------------------- the report


_T0 = datetime(2026, 11, 26, 9, 0, 0, tzinfo=UTC)


def _ev(seq: int, name: str, turn_index: int, **payload) -> dict:
    return {"id": seq, "event": name, "turn_index": turn_index, "payload": payload, "created_at": _T0 + timedelta(seconds=seq)}


def _call(seq: int, purpose: str, ms: int = 1000, tokens=(100, 20)) -> dict:
    return {"id": seq, "purpose": purpose, "model": "m", "status": "ok", "duration_ms": ms,
            "input_tokens": tokens[0], "output_tokens": tokens[1], "created_at": _T0 + timedelta(seconds=seq)}


def _two_turns() -> tuple[list[dict], list[dict]]:
    events = [
        _ev(0, "voice_session_started", 0),
        _ev(1, "voice_reply_started", 0, kind="question", since_speech_end_ms=None),       # opening: no turn
        # turn 1: early ack first, then the reply; speculation reused
        _ev(10, "voice_anticipation_ready", 0, latency_ms=900),
        _ev(11, "voice_anticipation_reused", 0),
        _ev(12, "voice_answer_submitted", 0, since_speech_end_ms=700, transcription_delay_ms=400, end_of_turn_delay_ms=650, words=12, fragments=1),
        _ev(13, "voice_early_acknowledgment", 0, after_ms=800),
        _ev(14, "voice_reply_started", 0, kind="early_ack", since_speech_end_ms=1500, since_submit_ms=800),
        _ev(15, "voice_tts_metrics", 0, kind="early_ack", ttfb_ms=300),
        _ev(16, "voice_core_completed", 0, core_ms=2600, action="PROBE"),
        _ev(17, "voice_reply_started", 1, kind="question", since_speech_end_ms=3500, since_submit_ms=2800, since_core_ms=200),
        _ev(18, "voice_tts_metrics", 1, kind="question", ttfb_ms=350),
        # turn 2: a filler, speculation discarded, late speech after submit
        _ev(20, "voice_anticipation_discarded", 1, reason="stale"),
        _ev(21, "voice_answer_submitted", 1, since_speech_end_ms=900, transcription_delay_ms=500, end_of_turn_delay_ms=900, words=30, fragments=2),
        _ev(22, "voice_processing_filler", 1, after_s=2.0),
        _ev(23, "voice_reply_started", 1, kind="filler", since_speech_end_ms=2900, since_submit_ms=2000),
        _ev(24, "voice_late_speech_captured", 1, fragments=1, words=5),
        _ev(25, "voice_core_completed", 1, core_ms=4100, action="NEXT_QUESTION"),
        _ev(26, "voice_reply_started", 2, kind="question", since_speech_end_ms=5200, since_submit_ms=4300, since_core_ms=200),
        _ev(27, "voice_tts_metrics", 2, kind="question", ttfb_ms=450),
    ]
    calls = [
        _call(9, "lab.preview_decision", 900),            # speculation for turn 1 (before submit)
        _call(15, "response_generation.decide", 2400),    # turn 1 canonical
        _call(19, "lab.preview_decision", 1000),          # speculation for turn 2, discarded
        _call(24, "response_generation.decide", 3900),    # turn 2 canonical
    ]
    return events, calls


def test_turns_are_split_at_each_submit_and_the_opening_is_not_a_turn() -> None:
    events, _ = _two_turns()
    turns = metrics.split_turns(events)
    assert [t["turn_index"] for t in turns] == [0, 1]
    # Speculation precedes the submit it is about and is carried into that turn.
    assert [e["event"] for e in turns[0]["events"]][:3] == [
        "voice_anticipation_ready", "voice_anticipation_reused", "voice_answer_submitted",
    ]
    assert [e["event"] for e in turns[1]["events"]][:2] == ["voice_anticipation_discarded", "voice_answer_submitted"]
    assert len(turns[0]["events"]) == 9 and len(turns[1]["events"]) == 8


def test_ttfa_counts_the_first_utterance_but_ttmr_only_the_official_reply() -> None:
    events, calls = _two_turns()
    report = metrics.summarize(events, calls)
    per_turn = report["per_turn"]
    assert per_turn[0]["ttfa_ms"] == 1500 and per_turn[0]["ttfa_kind"] == "early_ack"
    assert per_turn[0]["ttmr_ms"] == 3500
    assert per_turn[1]["ttfa_ms"] == 2900 and per_turn[1]["ttfa_kind"] == "filler"
    assert per_turn[1]["ttmr_ms"] == 5200
    assert report["ttfa_by_kind"] == {"early_ack": 1, "filler": 1}


def test_qwen_endpointing_core_and_vieneu_are_read_off_their_fields() -> None:
    events, calls = _two_turns()
    report = metrics.summarize(events, calls)
    assert report["transcription_delay_ms"]["p50"] == 400
    assert report["end_of_turn_delay_ms"]["p95"] == 900
    assert report["core_ms"]["n"] == 2 and report["core_ms"]["p99"] == 4100
    assert report["reply_after_core_ms"]["p50"] == 200
    assert report["tts_ttfb_ms_by_kind"]["question"]["p50"] == 350
    assert report["tts_ttfb_ms_by_kind"]["early_ack"]["n"] == 1


def test_calls_are_attributed_to_the_turn_they_were_speculating_about() -> None:
    events, calls = _two_turns()
    report = metrics.summarize(events, calls)
    assert [t["calls"] for t in report["per_turn"]] == [2, 2]
    assert [t["speculative_calls"] for t in report["per_turn"]] == [1, 1]
    assert report["calls_per_turn"] == 2.0
    assert report["speculative_calls"] == 2
    assert report["tokens"] == {"in": 400, "out": 80}


def test_anticipation_hit_rate_wasted_speculation_and_false_cut_proxy() -> None:
    events, calls = _two_turns()
    report = metrics.summarize(events, calls)
    assert report["anticipation"] == {"reused": 1, "discarded": 1, "ready": 1, "hit_rate": 0.5, "discard_reasons": {"stale": 1}}
    assert report["wasted_speculation"] == 1
    assert report["early_ack_available"] == 1
    assert report["false_cut_proxy"] == {"turns": 1, "rate": 0.5}


def test_the_endpointing_chain_and_pre_submit_events_belong_to_the_answer_they_preceded() -> None:
    """The nudge's audio, the STT finish and the prefetch all happen before the
    submit; the pilot's report attributed them to the previous turn."""
    events = [
        _ev(0, "voice_session_started", 0),
        _ev(1, "voice_reply_started", 0, kind="question", since_speech_end_ms=None),
        _ev(2, "voice_prefetch", 0, ok=True, ms=12),
        _ev(3, "voice_stt_diagnostic", 0, phase="chunk", processing_ms=430),
        _ev(4, "voice_stt_diagnostic", 0, phase="finish", processing_ms=430, pcm_speech_end_to_final_ms=1709),
        _ev(5, "voice_answer_pause_nudge", 0, after_s=1.25),
        _ev(6, "voice_reply_started", 0, kind="filler", since_speech_end_ms=2600),
        _ev(7, "voice_tts_metrics", 0, kind="filler", ttfb_ms=12),
        _ev(8, "voice_answer_submitted", 0, since_speech_end_ms=2860, words=40, fragments=2),
        _ev(9, "voice_endpointing", 0, anchor="transcript", commit_delay_ms=1800, effective_delay_ms=1800,
            vad_to_final_ms=276, final_to_turn_ms=804, turn_to_submit_ms=1801, since_speech_end_ms=2860,
            fragments=2, nudged=True),
        _ev(10, "voice_core_completed", 0, core_ms=3700, action="PROBE"),
        _ev(11, "voice_filler_interrupted", 0),
        _ev(12, "voice_reply_started", 1, kind="question", since_speech_end_ms=7000, since_submit_ms=4100, since_core_ms=450),
    ]
    turns = metrics.split_turns(events)
    assert len(turns) == 1
    assert [e["event"] for e in turns[0]["events"]][:6] == [
        "voice_prefetch", "voice_stt_diagnostic", "voice_stt_diagnostic", "voice_answer_pause_nudge",
        "voice_reply_started", "voice_tts_metrics",
    ]
    report = metrics.summarize(events, [])
    turn = report["per_turn"][0]
    assert turn["ttfa_ms"] == 2600 and turn["ttfa_kind"] == "filler", "the nudge is the first audio of this answer"
    assert turn["ttmr_ms"] == 7000
    assert (turn["vad_to_final_ms"], turn["final_to_turn_ms"], turn["turn_to_submit_ms"]) == (276, 804, 1801)
    assert turn["qwen_pcm_end_to_final_ms"] == 1709 and turn["nudged"] is True
    assert turn["filler_interrupted"] == 1 and turn["prefetch_ms"] == 12
    assert report["endpointing"]["anchors"] == {"transcript": 1} and report["endpointing"]["nudged_turns"] == 1
    assert report["endpointing"]["qwen_pcm_end_to_final_ms"]["p50"] == 1709
    assert report["filler_interrupted"] == 1 and report["prefetch_ms"]["p50"] == 12
    text = metrics.render(report)
    assert "VAD end → final transcript" in text and "commit anchor" in text


def test_backchannel_inferences_and_units_are_measured_per_answer() -> None:
    """Brief 2026-11-26: Talker time to first token, generation latency, stale
    rate, candidate overlaps, processing acks and the Talker's own calls."""
    events = [
        _ev(0, "voice_session_started", 0),
        _ev(1, "voice_reply_started", 0, kind="question", since_speech_end_ms=None),
        # listening-phase backchannel: before the submit, belongs to this answer
        _ev(2, "voice_backchannel_spoken", 0, phase="listening", kind="reflective", words=4, ttft_ms=320, ready_ms=410, held_ms=0),
        _ev(3, "voice_reply_started", 0, kind="backchannel", since_speech_end_ms=900),
        _ev(4, "voice_backchannel", 0, phase="listening", kind="reflective", ok=True, ttft_ms=320, total_ms=480, units=1, rejected=0, spoken=1),
        _ev(5, "voice_backchannel_overlap", 0),
        _ev(6, "voice_answer_submitted", 0, since_speech_end_ms=2000, words=40, fragments=2),
        _ev(7, "voice_backchannel_discarded", 0, reason="reply_ready", kind="processing", held_ms=0),
        _ev(8, "voice_backchannel", 0, phase="processing", kind="processing", ok=True, ttft_ms=350, total_ms=700, units=1, rejected=0, spoken=0),
        _ev(9, "voice_core_completed", 0, core_ms=3000, action="PROBE"),
        _ev(10, "voice_reply_started", 1, kind="question", since_speech_end_ms=6000, since_submit_ms=4000, since_core_ms=450),
    ]
    calls = [_call(4, "interviews.agent.talker.backchannel", 480, tokens=(400, 12)),
             _call(8, "interviews.agent.talker.backchannel", 700, tokens=(420, 10)),
             _call(9, "interviews.agent.decide", 2800, tokens=(2500, 150))]
    report = metrics.summarize(events, calls)
    turn = report["per_turn"][0]
    assert turn["ttfa_ms"] == 900 and turn["ttfa_kind"] == "backchannel", "the listening backchannel is this answer's first audio"
    assert turn["ttmr_ms"] == 6000
    assert turn["backchannel_inferences"] == 2 and turn["backchannel_spoken"] == 1 and turn["backchannel_discarded"] == 1
    bc = report["backchannel"]
    assert bc["inferences"] == 2 and bc["spoken"] == 1 and bc["discarded"] == 1 and bc["stale_rate"] == 0.5
    assert bc["overlaps"] == 1 and bc["processing_spoken"] == 0 and bc["kinds"] == {"reflective": 1}
    assert bc["ttft_ms"]["p50"] == 320 and bc["total_ms"]["n"] == 2
    assert bc["talker_calls"] == 2 and bc["talker_tokens"] == {"in": 820, "out": 22}
    text = metrics.render(report)
    assert "Talker time to first token" in text and "stale rate" in text


def test_percentiles_are_nearest_rank_and_empty_is_none() -> None:
    assert metrics.percentile([], 50) is None
    assert metrics.percentile([5.0], 99) == 5.0
    values = [float(v) for v in range(1, 101)]
    assert metrics.percentile(values, 50) == 50.0
    assert metrics.percentile(values, 95) == 95.0
    assert metrics.percentile(values, 99) == 99.0


def test_render_is_readable_without_data() -> None:
    text = metrics.render(metrics.summarize([], []))
    assert "turns: 0" in text and "TTFA" in text and "TTMR" in text
