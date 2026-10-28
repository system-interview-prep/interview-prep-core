import asyncio
from datetime import UTC, datetime

import pytest

from src.modules.interviews.adapters.voice import lab
from src.modules.interviews.adapters.voice.voice_events import (
    VOICE_EVENTS,
    VoiceEventReporter,
    sanitize_fields,
)

T0 = datetime(2026, 10, 19, 9, 0, tzinfo=UTC)


def test_sanitize_keeps_numbers_flags_and_short_codes_only():
    clean = sanitize_fields(
        {
            "words": 12,
            "confidence": 0.8123456,
            "terminal": True,
            "reason": "x" * 500,
            "transcript": "what the candidate said",
            "text": "never",
            "nested": {"a": 1},
            "Bad-Key": 1,
            "nan": float("nan"),
            "none": None,
        }
    )
    assert clean == {"words": 12, "confidence": 0.8123, "terminal": True, "reason": "x" * 120, "none": None}


def test_sanitize_caps_field_count():
    assert len(sanitize_fields({f"k{i}": i for i in range(30)})) == 12


def test_reporter_batches_dedupes_state_and_ignores_unknown_events():
    batches: list[list[dict]] = []

    async def post(batch):
        batches.append(batch)

    async def run():
        reporter = VoiceEventReporter(post, flush_delay=0.01, now=lambda: T0)
        reporter.state("ai_speaking", kind="question")
        reporter.state("ai_speaking", kind="question")  # same state: ignored
        reporter.state("ai_speaking", kind="replay")  # new kind: reported
        reporter.state("not_a_state")
        reporter.emit("voice_not_heard")
        reporter.emit("made_up_event")
        await asyncio.sleep(0.05)
        await reporter.aclose()
        reporter.emit("voice_not_heard")  # closed: dropped
        return reporter

    reporter = asyncio.run(run())
    events = [item for batch in batches for item in batch]
    assert [e["event"] for e in events] == ["voice_state", "voice_state", "voice_not_heard"]
    assert events[0]["fields"] == {"state": "ai_speaking", "kind": "question"}
    assert events[1]["fields"]["kind"] == "replay"
    assert events[0]["at"] == T0.isoformat()
    assert reporter.current_state == "ai_speaking"


def test_reporter_never_raises_when_posting_fails():
    async def post(_batch):
        raise RuntimeError("API down")

    async def run():
        reporter = VoiceEventReporter(post, flush_delay=0.01)
        reporter.emit("voice_core_failed", error_type="ReadTimeout")
        await asyncio.sleep(0.05)
        await reporter.aclose()

    asyncio.run(run())


def test_reporter_without_loop_is_a_no_op_until_flushed():
    sent: list[list[dict]] = []

    async def post(batch):
        sent.append(batch)

    reporter = VoiceEventReporter(post)
    reporter.emit("voice_not_heard")  # no running loop: queued, not scheduled
    asyncio.run(reporter.flush())
    assert [e["event"] for e in sent[0]] == ["voice_not_heard"]


def test_reporter_queue_is_bounded():
    sent: list[list[dict]] = []

    async def post(batch):
        sent.append(batch)

    reporter = VoiceEventReporter(post, max_queue=3, max_batch=2)
    for _ in range(5):
        reporter.emit("voice_not_heard")
    asyncio.run(reporter.flush())
    assert [len(b) for b in sent] == [2, 1]


class _Result:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _Db:
    async def execute(self, *_args, **_kwargs):
        return _Result(3)


def test_runtime_events_endpoint_records_known_events_on_the_current_turn(monkeypatch):
    recorded = []
    monkeypatch.setattr(
        lab, "trace_event", lambda workflow, event, **fields: recorded.append((workflow, event, fields))
    )
    payload = lab.VoiceRuntimeEvents(
        events=[
            lab.VoiceRuntimeEvent(
                event="voice_state", at=T0, fields={"state": "candidate_speaking", "session_id": "evil"}
            ),
            lab.VoiceRuntimeEvent(
                event="voice_unclear_transcript", fields={"reason": "low_confidence", "transcript": "x"}
            ),
            lab.VoiceRuntimeEvent(event="unknown_event"),
        ]
    )
    result = asyncio.run(
        lab.record_livekit_runtime_events(payload, claims={"session_id": "s1", "user_id": "u1"}, db=_Db())
    )
    assert result == {"accepted": 2}
    assert [(w, e) for w, e, _ in recorded] == [
        ("voice", "voice_state"),
        ("voice", "voice_unclear_transcript"),
    ]
    first = recorded[0][2]
    assert first["session_id"] == "s1" and first["turn_index"] == 3 and first["state"] == "candidate_speaking"
    assert first["agent_at"] == T0.isoformat()
    assert "transcript" not in recorded[1][2]


def test_every_reported_event_is_in_the_allow_list():
    import inspect

    from src.modules.interviews.adapters.voice import livekit_agent

    source = inspect.getsource(livekit_agent)
    import re

    names = set(re.findall(r'(?:report|emit)\(\s*"(voice_[a-z_]+)"', source))
    assert names, "expected voice events in the agent"
    assert names <= VOICE_EVENTS, names - VOICE_EVENTS


@pytest.mark.parametrize("path", ["livekit/runtime/events"])
def test_runtime_events_require_the_voice_credential(client, path):
    assert client.post(f"/ai/voice-lab/{path}", json={"events": []}).status_code == 401
