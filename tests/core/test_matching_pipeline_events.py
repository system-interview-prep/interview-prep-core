"""Matching run events: routed to their own buffer only while a run is active."""

import asyncio
import json

import pytest
from fastapi.concurrency import run_in_threadpool

from src.core import pipeline_events as pe
from src.modules.matching.application import run_tracking


@pytest.fixture
def recorder(monkeypatch):
    """Recording on, no flusher thread, empty buffers."""
    monkeypatch.setattr(pe, "enabled", lambda: True)
    monkeypatch.setattr(pe, "_ensure_thread", lambda: None)
    buffers = (pe._recorder.events, pe._recorder.calls, pe._recorder.match_events, pe._recorder.match_calls)
    for buffer in buffers:
        buffer.clear()
    session_token = pe.current_session_id.set(None)
    run_token = pe.current_match_run.set(None)
    yield pe._recorder
    pe.current_match_run.reset(run_token)
    pe.current_session_id.reset(session_token)
    for buffer in buffers:
        buffer.clear()


def test_matching_events_without_a_run_are_dropped(recorder):
    pe.record_event("matching", "match_scored", {"decision": "assessed"})
    pe.record_event("clarification", "rescore_completed", {"answers_count": 1})
    assert not recorder.match_events and not recorder.events


def test_matching_events_go_to_the_matching_buffer_during_a_run(recorder):
    with run_tracking.match_run("match", user_id="u1", resume_id="cv-1", job_id="job-1") as run:
        run.cache_key = "k" * 64
        pe.record_event("matching", "match_scored", {"decision": "assessed", "duration_ms": 12})
        pe.record_event("clarification", "clarification_gate_completed", {"approved_count": 1})
    assert not recorder.events
    scored, gate = recorder.match_events
    assert scored["run_id"] == run.run_id and scored["flow"] == "match"
    assert (scored["user_id"], scored["resume_id"], scored["job_id"]) == ("u1", "cv-1", "job-1")
    assert scored["cache_key"] == "k" * 64
    assert scored["stage"] == "scoring" and scored["status"] == "completed" and scored["duration_ms"] == 12
    assert gate["stage"] == "clarify"
    assert pe.current_match_run.get() is None


def test_per_requirement_and_legacy_matching_events_are_not_persisted(recorder):
    with run_tracking.match_run("match"):
        pe.record_event("matching", "requirement_evaluated", {"requirement_id": "r1"})
        pe.record_event("matching", "targeted_reparse", {"status": "x"})
        pe.record_event("clarification", "jev_analysis_started", {"requirement_id": "r1"})
    assert not recorder.match_events


def test_interview_events_are_unchanged_during_a_match_run(recorder):
    with run_tracking.match_run("planner"):
        pe.record_event("interviewer", "plan_started", {"session_id": "s1"})
    assert not recorder.match_events
    (row,) = recorder.events
    assert row["session_id"] == "s1" and row["stage"] == "plan" and row["status"] == "started"
    assert set(row) == {
        "session_id",
        "stage",
        "event",
        "status",
        "turn_index",
        "duration_ms",
        "payload",
        "created_at",
    }


def test_matching_status_of():
    assert pe.matching_status_of("cache_lookup_completed", {"event_status": "info"}) == "info"
    assert pe.matching_status_of("cache_lookup_completed", {"event_status": "bogus"}) == "completed"
    assert pe.matching_status_of("targeted_reparse_skipped", {}) == "skipped"
    assert pe.matching_status_of("match_failed", {}) == "error"
    assert pe.matching_status_of("inputs_resolved", {"error_type": "X"}) == "error"
    assert pe.matching_status_of("match_requested", {}) == "started"
    assert pe.matching_status_of("match_enqueued", {}) == "info"


def test_sanitizer_never_persists_text_bearing_keys(recorder):
    fields = {
        "raw_text": "Full CV text",
        "exact_quote": "a verbatim CV passage",
        "answer_text": "the candidate's answer",
        "question_text": "a generated question",
        "email": "candidate@example.test",
        "user_id": "u1",
        "event_status": "info",
        "hit": True,
        "items": [{"requirement_id": "r1", "label": "Java", "text": "evidence", "quote": "q"}],
        "nested": {"evidence_texts": ["x"], "count": 2},
    }
    with run_tracking.match_run("match"):
        pe.record_event("matching", "cache_lookup_completed", fields)
    payload = json.loads(recorder.match_events[0]["payload"])
    assert payload == {
        "hit": True,
        "items": [{"requirement_id": "r1", "label": "Java"}],
        "nested": {"count": 2},
    }
    assert recorder.match_events[0]["status"] == "info"
    for secret in ("Full CV text", "verbatim", "candidate's answer", "generated question", "example.test"):
        assert secret not in recorder.match_events[0]["payload"]


def test_requirements_event_keeps_up_to_sixty_items(recorder):
    items = [{"requirement_id": f"r{i}"} for i in range(75)]
    with run_tracking.match_run("match"):
        pe.record_event("matching", "requirements_evaluated", {"items": items})
    assert len(json.loads(recorder.match_events[0]["payload"])["items"]) == 60


def test_ai_calls_are_stamped_with_the_active_run(recorder):
    pe.record_ai_call(purpose="outside", provider="openai", model="m", status="ok", duration_ms=1)
    with run_tracking.match_run("match") as run:
        pe.record_ai_call(purpose="inside", provider="openai", model="m", status="ok", duration_ms=2)
    (outside,) = recorder.calls
    (inside,) = recorder.match_calls
    assert "match_run_id" not in outside and outside["purpose"] == "outside"
    assert inside["match_run_id"] == run.run_id and inside["purpose"] == "inside"


async def test_run_reaches_threadpool_calls(recorder):
    with run_tracking.match_run("clarify_full") as run:
        await run_in_threadpool(
            pe.record_ai_call, purpose="p", provider="openai", model="m", status="ok", duration_ms=1
        )
        await run_in_threadpool(pe.record_event, "matching", "match_scored", {})
    assert recorder.match_calls[0]["match_run_id"] == run.run_id
    assert recorder.match_events[0]["run_id"] == run.run_id


def test_matching_write_failure_never_drops_interview_rows(recorder, monkeypatch):
    written = {}

    async def failing_matching(*_args, **_kwargs):
        raise RuntimeError('relation "matching_pipeline_events" does not exist')

    async def ok_write(events, calls, *, retention, summaries=None):
        written.setdefault("batches", []).append((list(events), list(calls)))

    monkeypatch.setattr(pe, "_write_matching", failing_matching)
    monkeypatch.setattr(pe, "_write", ok_write)
    pe.record_event("interviewer", "plan_started", {"session_id": "s1"})
    with run_tracking.match_run("match"):
        pe.record_event("matching", "match_scored", {})
        pe.record_ai_call(purpose="p", provider="openai", model="m", status="ok", duration_ms=1)

    assert asyncio.run(pe.flush_once()) == 2
    fallback_calls = written["batches"][0][1]
    # The run-stamped call is kept, without the column the old schema lacks.
    assert len(fallback_calls) == 1 and "match_run_id" not in fallback_calls[0]
    interview_events = written["batches"][1][0]
    assert [e["event"] for e in interview_events] == ["plan_started"]


def test_matching_rows_are_written_in_their_own_transaction(recorder, monkeypatch):
    seen = {}

    async def ok_matching(events, calls, *, retention):
        seen["matching"] = (len(events), len(calls), retention)

    async def ok_write(events, calls, *, retention, summaries=None):
        seen["interview"] = (len(events), len(calls))

    monkeypatch.setattr(pe, "_write_matching", ok_matching)
    monkeypatch.setattr(pe, "_write", ok_write)
    with run_tracking.match_run("match"):
        pe.record_event("matching", "match_completed", {"decision": "assessed"})
        pe.record_ai_call(purpose="p", provider="openai", model="m", status="ok", duration_ms=1)
    assert asyncio.run(pe.flush_once()) == 2
    assert seen["matching"] == (1, 1, False)
    # Run-stamped calls are still live-summarised (written with no new interview rows).
    assert seen["interview"] == (0, 0)
