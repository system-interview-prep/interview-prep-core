"""CV pipeline events: routed to their own buffer only while a CV context is active."""

import asyncio
import json

import pytest
from fastapi.concurrency import run_in_threadpool

from src.core import pipeline_events as pe
from src.modules.user_cvs.application import run_tracking


@pytest.fixture
def recorder(monkeypatch):
    """Recording on, no flusher thread, empty buffers."""
    monkeypatch.setattr(pe, "enabled", lambda: True)
    monkeypatch.setattr(pe, "_ensure_thread", lambda: None)
    buffers = (
        pe._recorder.events,
        pe._recorder.calls,
        pe._recorder.match_events,
        pe._recorder.match_calls,
        pe._recorder.cv_events,
        pe._recorder.cv_calls,
    )
    for buffer in buffers:
        buffer.clear()
    tokens = (pe.current_session_id.set(None), pe.current_match_run.set(None), pe.current_cv_run.set(None))
    yield pe._recorder
    pe.current_cv_run.reset(tokens[2])
    pe.current_match_run.reset(tokens[1])
    pe.current_session_id.reset(tokens[0])
    for buffer in buffers:
        buffer.clear()


def test_cv_events_without_a_context_are_dropped(recorder):
    pe.record_event("cv_parser", "completed", {"cv_id": "cv-1"})
    pe.record_event("document_extractor", "provider_failed", {"provider": "mineru"})
    pe.record_event("cv_upload", "cv_uploaded", {"size_bytes": 1})
    assert not recorder.cv_events and not recorder.events


def test_cv_events_go_to_the_cv_buffer_with_their_stage_and_status(recorder):
    with run_tracking.cv_run("cv-1", user_id="user-1", attempt_id="a1") as run:
        pe.record_event("cv_parser", "extraction_started", {"cv_id": "cv-1", "bytes": 10})
        pe.record_event("document_extractor", "provider_failed", {"provider": "mineru", "error_type": "X"})
        pe.record_event("cv_parser", "quality_gate", {"status": "review_required"})
        pe.record_event("cv_parser", "quality_gate", {"status": "ready"})
        pe.record_event("cv_parser", "failed", {"error_type": "ValueError", "duration_ms": 30})
    assert run.attempt_id == "a1" and pe.current_cv_run.get() is None
    rows = list(recorder.cv_events)
    assert [(r["stage"], r["event"], r["status"]) for r in rows] == [
        ("extract", "extraction_started", "started"),
        ("extract", "provider_failed", "warning"),
        ("quality", "quality_gate", "warning"),
        ("quality", "quality_gate", "completed"),
        ("done", "failed", "error"),
    ]
    assert {(r["cv_id"], r["attempt_id"], r["user_id"]) for r in rows} == {("cv-1", "a1", "user-1")}
    assert rows[-1]["duration_ms"] == 30
    assert not recorder.events


def test_only_whitelisted_cv_events_are_persisted(recorder):
    with run_tracking.cv_run("cv-1"):
        pe.record_event("cv_parser", "ignored", {})
        pe.record_event("cv_parser", "some_debug_event", {})
        pe.record_event("jd_parser", "completed", {})
    assert not recorder.cv_events


def test_sanitizer_never_persists_cv_text_or_error_messages(recorder):
    with run_tracking.cv_run("cv-1"):
        pe.record_event(
            "cv_parser",
            "retry_scheduled",
            {
                "cv_id": "cv-1",
                "error": "Timeout while reading 'Nguyen Van A, an@example.com'",
                "error_type": "TimeoutError",
                "filename": "Nguyen Van A.pdf",
                "message": "quoted cv text",
                "text": "cv text",
                "email": "an@example.com",
                "warnings": [
                    {"code": "x", "path": "employment[0]", "message": "quote", "section": "employment"}
                ],
            },
        )
    payload = json.loads(recorder.cv_events[0]["payload"])
    assert payload == {
        "error_type": "TimeoutError",
        "warnings": [{"code": "x", "section": "employment"}],
    }
    assert recorder.cv_events[0]["status"] == "warning"


def test_ai_calls_are_stamped_and_counted_during_a_cv_parse(recorder):
    with run_tracking.cv_run("cv-1") as run:
        pe.record_ai_call(
            purpose="user_cvs.hybrid.p", provider="openai", model="m", status="ok", duration_ms=1
        )
        pe.record_ai_call(
            purpose="user_cvs.hybrid.p", provider="openai", model="m", status="error", duration_ms=2
        )
    pe.record_ai_call(purpose="other", provider="openai", model="m", status="ok", duration_ms=1)
    assert [c["cv_id"] for c in recorder.cv_calls] == ["cv-1", "cv-1"]
    assert (run.ai_calls, run.ai_errors) == (2, 1)
    assert len(recorder.calls) == 1 and "cv_id" not in recorder.calls[0]


def test_a_match_run_takes_precedence_for_ai_calls(recorder):
    from src.modules.matching.application import run_tracking as matching_tracking

    with run_tracking.cv_run("cv-1"), matching_tracking.match_run("match"):
        pe.record_ai_call(purpose="p", provider="openai", model="m", status="ok", duration_ms=1)
    assert len(recorder.match_calls) == 1 and not recorder.cv_calls


async def test_cv_context_reaches_threadpool_calls(recorder):
    with run_tracking.cv_run("cv-1"):
        await run_in_threadpool(
            pe.record_ai_call, purpose="p", provider="openai", model="m", status="ok", duration_ms=1
        )
    assert recorder.cv_calls[0]["cv_id"] == "cv-1"


def test_cv_rows_are_written_in_their_own_transaction(recorder, monkeypatch):
    seen = {}

    async def ok_cv(events, calls, *, retention):
        seen["cv"] = (len(events), len(calls), retention)

    async def ok_write(events, calls, *, retention, summaries=None):
        seen["interview"] = (len(events), len(calls), len(summaries or []))

    monkeypatch.setattr(pe, "_write_cv", ok_cv)
    monkeypatch.setattr(pe, "_write", ok_write)
    with run_tracking.cv_run("cv-1"):
        pe.record_event("cv_parser", "completed", {"canonical_status": "ready"})
        pe.record_ai_call(purpose="p", provider="openai", model="m", status="ok", duration_ms=1)
    assert asyncio.run(pe.flush_once()) == 2
    assert seen["cv"] == (1, 1, False)
    assert seen["interview"] == (0, 0, 1)
    assert not pe.has_pending()


def test_cv_write_failure_keeps_the_ai_calls_without_the_new_column(recorder, monkeypatch):
    written = []

    async def failing_cv(*_args, **_kwargs):
        raise RuntimeError('relation "cv_pipeline_events" does not exist')

    async def ok_write(events, calls, *, retention, summaries=None):
        written.append((list(events), list(calls)))

    monkeypatch.setattr(pe, "_write_cv", failing_cv)
    monkeypatch.setattr(pe, "_write", ok_write)
    pe.record_event("interviewer", "plan_started", {"session_id": "s1"})
    with run_tracking.cv_run("cv-1"):
        pe.record_event("cv_parser", "completed", {})
        pe.record_ai_call(purpose="p", provider="openai", model="m", status="ok", duration_ms=1)
    assert asyncio.run(pe.flush_once()) == 2
    fallback_calls = written[0][1]
    assert len(fallback_calls) == 1 and "cv_id" not in fallback_calls[0]
    assert [e["event"] for e in written[1][0]] == ["plan_started"]


def test_flush_pending_writes_the_buffer_synchronously(recorder, monkeypatch):
    seen = []

    async def ok_cv(events, calls, *, retention):
        seen.append(len(events))

    async def ok_write(*_args, **_kwargs):
        return None

    monkeypatch.setattr(pe, "_write_cv", ok_cv)
    monkeypatch.setattr(pe, "_write", ok_write)
    pe.flush_pending()  # nothing buffered: no write
    with run_tracking.cv_run("cv-1"):
        pe.record_event("cv_parser", "completed", {})
    pe.flush_pending()
    assert seen == [1]
