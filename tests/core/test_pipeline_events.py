import json

import pytest

from src.core import pipeline_events as pe


@pytest.fixture
def recorder(monkeypatch):
    """Recording on, no flusher thread, empty buffers."""
    monkeypatch.setattr(pe, "enabled", lambda: True)
    monkeypatch.setattr(pe, "_ensure_thread", lambda: None)
    pe._recorder.events.clear()
    pe._recorder.calls.clear()
    token = pe.current_session_id.set(None)
    yield pe._recorder
    pe.current_session_id.reset(token)
    pe._recorder.events.clear()
    pe._recorder.calls.clear()


@pytest.mark.parametrize(
    ("event", "stage"),
    [
        ("session_created", "session"),
        ("plan_completed", "plan"),
        ("plan_blocked_by_match_threshold", "plan"),
        ("question_selection_started", "questions"),
        ("spoken_questions_saved", "questions"),
        ("question_generated", "questions"),
        ("turn_spoken", "interview"),
        ("probe_guardrail_fallback_triggered", "interview"),
        ("candidate_message_processed", "interview"),
        ("evaluation_job_claimed", "evaluation"),
        ("turn_graded", "evaluation"),
        ("something_else", "other"),
    ],
)
def test_stage_of(event, stage):
    assert pe.stage_of(event) == stage


def test_status_of():
    assert pe.status_of("plan_failed", {}) == "error"
    assert pe.status_of("turn_spoken", {"error_type": "X"}) == "error"
    assert pe.status_of("plan_started", {}) == "started"
    assert pe.status_of("plan_completed", {}) == "completed"
    assert pe.status_of("probe_guardrail_fallback_triggered", {}) == "warning"
    assert pe.status_of("evaluation_decision_capped", {}) == "warning"
    assert pe.status_of("turn_spoken", {}) == "info"


def test_event_drops_candidate_words_and_truncates(recorder):
    pe.record_event(
        "naturalness",
        "turn_spoken",
        {
            "session_id": "s1",
            "turn_index": 2,
            "candidate": "secret answer",
            "interviewer": "x" * 2000,
            "core_ms": 812,
        },
    )
    row = recorder.events[0]
    payload = json.loads(row["payload"])
    assert row["session_id"] == "s1" and row["stage"] == "interview" and row["turn_index"] == 2
    assert row["duration_ms"] == 812
    assert "candidate" not in payload and "session_id" not in payload
    assert len(payload["interviewer"]) <= 600


def test_events_without_session_inherit_the_request_session(recorder):
    pe.record_event("interviewer", "probe_guardrail_fallback_triggered", {"reason": "x"})
    assert not recorder.events  # nothing to attach it to yet
    pe.record_event("interviewer", "plan_started", {"session_id": "s9"})
    pe.record_event("interviewer", "question_generated", {"concept_id": "c"})
    assert [r["session_id"] for r in recorder.events] == ["s9", "s9"]


def test_other_workflows_are_not_persisted(recorder):
    pe.record_event("cv_parser", "parse_completed", {"session_id": "s1"})
    assert not recorder.events


def test_disabled_records_nothing(monkeypatch, recorder):
    monkeypatch.setattr(pe, "enabled", lambda: False)
    pe.record_event("interviewer", "plan_started", {"session_id": "s1"})
    pe.record_ai_call(purpose="p", provider="openai", model="m", status="ok", duration_ms=1)
    assert not recorder.events and not recorder.calls


class _Usage:
    input_tokens = 120
    output_tokens = 30


def test_ai_call_timer_records_ok_and_error(recorder, monkeypatch):
    from src.core.config import get_settings

    monkeypatch.setattr(get_settings(), "ai_call_log_capture_text", False)
    pe.current_session_id.set("s2")
    with pe.ai_call_timer("engine.evaluate", provider="openai", model="gpt", input_text="cv text") as call:
        call.done("output", _Usage())
    with pytest.raises(RuntimeError), pe.ai_call_timer("engine.probe", provider="openai", model="gpt"):
        raise RuntimeError("timeout")
    ok, err = recorder.calls
    assert ok["status"] == "ok" and ok["session_id"] == "s2"
    assert (ok["input_tokens"], ok["output_tokens"], ok["input_chars"], ok["output_chars"]) == (120, 30, 7, 6)
    # Text is not kept unless capture is configured.
    assert ok["input_preview"] is None and ok["output_preview"] is None
    assert (
        err["status"] == "error" and err["error_type"] == "RuntimeError" and err["error_message"] == "timeout"
    )


def test_ai_call_timer_captures_text_when_configured(recorder, monkeypatch):
    from src.core.config import get_settings

    monkeypatch.setattr(get_settings(), "ai_call_log_capture_text", True)
    with pe.ai_call_timer("x", provider="openai", model="gpt", input_text="in") as call:
        call.done("out")
    assert recorder.calls[0]["input_preview"] == "in" and recorder.calls[0]["output_preview"] == "out"


def _caller():
    return pe.caller_purpose(depth=1)


def test_caller_purpose_names_the_calling_function():
    assert _caller() == "test_pipeline_events._caller"


async def test_generate_text_logs_purpose_of_caller(recorder, monkeypatch):
    from src.modules.ai import facade

    class _Response:
        output_text = " hi "
        usage = _Usage()

    class _Client:
        class responses:  # noqa: N801
            @staticmethod
            def create(**_):
                return _Response()

    monkeypatch.setattr(facade, "_client", lambda: _Client())

    async def evaluate_answer():
        return await facade.generate_text(instructions="i", input_text="abc")

    assert await evaluate_answer() == "hi"
    call = recorder.calls[0]
    assert call["purpose"].endswith("evaluate_answer")
    assert call["provider"] == "openai" and call["output_tokens"] == 30


def test_voice_events_are_interview_stage_and_incidents_are_warnings():
    assert pe.stage_of("voice_state") == "interview"
    assert pe.status_of("voice_not_heard", {}) == "warning"
    assert pe.status_of("voice_provider_degraded", {}) == "warning"
    assert pe.status_of("voice_core_failed", {"error_type": "ReadTimeout"}) == "error"
    assert pe.status_of("voice_state", {}) == "info"


def test_voice_workflow_is_persisted(recorder):
    pe.record_event("voice", "voice_state", {"session_id": "s1", "state": "candidate_speaking"})
    assert recorder.events[0]["stage"] == "interview"


def test_live_summaries_group_by_session_and_count_problems():
    events = [
        {"session_id": "s1", "event": "turn_spoken", "stage": "interview", "status": "info", "turn_index": 1},
        {
            "session_id": "s1",
            "event": "voice_not_heard",
            "stage": "interview",
            "status": "warning",
            "turn_index": 1,
        },
        {"session_id": "s2", "event": "plan_failed", "stage": "plan", "status": "error", "turn_index": None},
    ]
    calls = [{"session_id": "s1", "status": "error"}, {"session_id": None, "status": "ok"}]
    by_id = {s["sessionId"]: s for s in pe.live_summaries(events, calls)}
    assert by_id["s1"]["problems"] == 2 and by_id["s1"]["aiCalls"] == 1 and by_id["s1"]["aiErrors"] == 1
    assert [e["event"] for e in by_id["s1"]["events"]] == ["turn_spoken", "voice_not_heard"]
    assert by_id["s2"]["problems"] == 1
    assert by_id[None]["aiCalls"] == 1 and by_id[None]["problems"] == 0


def test_notify_payload_stays_under_the_postgres_limit():
    summary = {
        "sessionId": "s1",
        "at": "now",
        "events": [
            {"event": "x" * 60, "stage": "interview", "status": "info", "turnIndex": i} for i in range(500)
        ],
        "aiCalls": 0,
        "aiErrors": 0,
        "problems": 0,
    }
    payload = pe.notify_payload(summary)
    body = json.loads(payload)
    assert len(payload.encode("utf-8")) <= 7000
    assert body["o"] == pe.INSTANCE_ID and body["events"][-1]["turnIndex"] == 499


def test_flush_notifies_live_listeners_only_after_a_successful_write(recorder, monkeypatch):
    import asyncio

    written = []
    seen = []

    async def ok_write(events, calls, *, retention, summaries=None):
        written.append(summaries)

    pe.add_live_listener(seen.append)
    try:
        monkeypatch.setattr(pe, "_write", ok_write)
        pe.record_event("interviewer", "turn_spoken", {"session_id": "s1", "turn_index": 0})
        assert asyncio.run(pe.flush_once()) == 1
        assert written[0][0]["sessionId"] == "s1"
        assert seen and seen[0][0]["sessionId"] == "s1"

        async def failing_write(*_args, **_kwargs):
            raise RuntimeError("db down")

        seen.clear()
        monkeypatch.setattr(pe, "_write", failing_write)
        pe.record_event("interviewer", "turn_spoken", {"session_id": "s1", "turn_index": 1})
        assert asyncio.run(pe.flush_once()) == 0
        assert seen == []
    finally:
        pe.remove_live_listener(seen.append)
