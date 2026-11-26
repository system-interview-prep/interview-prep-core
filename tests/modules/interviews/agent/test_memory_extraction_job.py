"""Post-session memory extraction as a durable background job (V2 spec §9).

The report is persisted first; extraction is dispatched to the existing
Celery/RabbitMQ worker; the job survives duplicate delivery, a lost worker,
and a refused dispatch, and never touches the grade.
"""

from __future__ import annotations

from typing import Any

import pytest
from langgraph.store.memory import InMemoryStore

from src.modules.interviews.graph import runtime as graph_runtime
from src.modules.interviews.memory import candidate_memory as cm
from src.modules.interviews.memory import extraction_job as job

REPORT = {"overall_score": 7.5, "competency_scores": [{"name": "Redis", "score": 7.0}]}


class _DB:
    """Only what the job asks of the database: the advisory lock."""

    def __init__(self, *, lock: bool = True) -> None:
        self.lock = lock
        self.statements: list[str] = []

    async def scalar(self, statement: Any, params: dict | None = None):
        self.statements.append(str(statement))
        assert "pg_try_advisory_xact_lock" in str(statement)
        return self.lock


@pytest.fixture
def readwrite(monkeypatch):
    monkeypatch.setattr(cm, "enabled", lambda: "readwrite")

    async def user_of(_db, _sid):
        return "u1"

    monkeypatch.setattr(cm, "_user_id_of_session", user_of)
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(job, "trace_event", lambda area, name, **kw: events.append((name, kw)))
    return events


@pytest.fixture
def extraction(monkeypatch):
    """The langmem run replaced by a recorder; the job's durability is what is under test."""
    calls: list[dict] = []

    async def fake(db, session_id, *, evaluation, store=None, raise_errors=False, **_):
        calls.append({"session_id": session_id, "evaluation": evaluation, "raise_errors": raise_errors})
        return {"inserted": 1, "patched": 0, "removed": 0, "truncated": 0}

    monkeypatch.setattr(job, "extract_after_session", fake)
    return calls


# ------------------------------------------------------------- the job


@pytest.mark.asyncio
async def test_a_first_delivery_extracts_and_records_done(readwrite, extraction) -> None:
    store = InMemoryStore()
    result = await job.run_memory_extraction_job(_DB(), "s1", store=store, evaluation=REPORT)
    assert result["status"] == "COMPLETE"
    assert extraction[0]["raise_errors"] is True, "the worker must see failures to retry them"
    marker = await job.job_marker(store, "u1", "s1")
    assert marker["status"] == "DONE"
    assert marker["counts"]["inserted"] == 1


@pytest.mark.asyncio
async def test_a_duplicate_delivery_does_not_extract_twice(readwrite, extraction) -> None:
    store = InMemoryStore()
    await job.run_memory_extraction_job(_DB(), "s1", store=store, evaluation=REPORT)
    again = await job.run_memory_extraction_job(_DB(), "s1", store=store, evaluation=REPORT)
    assert again["status"] == "SKIPPED" and again["reason"] == "already_extracted"
    assert len(extraction) == 1
    assert any(name == "candidate_memory_job_duplicate" for name, _ in readwrite)


@pytest.mark.asyncio
async def test_a_session_held_by_another_worker_is_retried_not_failed(readwrite, extraction) -> None:
    result = await job.run_memory_extraction_job(_DB(lock=False), "s1", store=InMemoryStore(), evaluation=REPORT)
    assert result["status"] == "RETRY"
    assert result["retry_in"] > 0
    assert extraction == []


@pytest.mark.asyncio
async def test_a_failed_extraction_is_marked_and_raised_for_retry(readwrite, monkeypatch) -> None:
    async def boom(*a, **k):
        raise RuntimeError("provider down")

    monkeypatch.setattr(job, "extract_after_session", boom)
    store = InMemoryStore()
    with pytest.raises(RuntimeError):
        await job.run_memory_extraction_job(_DB(), "s1", store=store, evaluation=REPORT, attempt=2)
    marker = await job.job_marker(store, "u1", "s1")
    assert marker["status"] == "FAILED" and marker["attempt"] == 2
    assert "provider down" in marker["error"]


@pytest.mark.asyncio
async def test_a_lost_worker_leaves_a_resumable_marker(readwrite, extraction) -> None:
    """Worker died after PROCESSING was written: the redelivery runs it again."""
    store = InMemoryStore()
    await job._mark(store, "u1", "s1", status="PROCESSING", attempt=1)
    result = await job.run_memory_extraction_job(_DB(), "s1", store=store, evaluation=REPORT, attempt=2)
    assert result["status"] == "COMPLETE"
    assert (await job.job_marker(store, "u1", "s1"))["status"] == "DONE"
    assert len(extraction) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "setup,reason",
    [
        (dict(evaluation=None), "evaluation_not_available"),
        (dict(store=None), "store_unavailable"),
    ],
)
async def test_nothing_to_extract_from_is_a_skip_not_an_error(readwrite, extraction, setup, reason) -> None:
    kwargs = {"store": InMemoryStore(), "evaluation": REPORT, **setup}
    result = await job.run_memory_extraction_job(_DB(), "s1", **kwargs)
    assert result["status"] == "SKIPPED" and result["reason"] == reason
    assert extraction == []


@pytest.mark.asyncio
async def test_memory_off_means_no_job_and_no_dispatch(monkeypatch, extraction) -> None:
    monkeypatch.setattr(cm, "enabled", lambda: "read")
    result = await job.run_memory_extraction_job(_DB(), "s1", store=InMemoryStore(), evaluation=REPORT)
    assert result["reason"] == "memory_disabled"
    assert job.enqueue_memory_extraction("s1") is False
    assert extraction == []


def test_a_refused_dispatch_is_reported_not_raised(monkeypatch) -> None:
    monkeypatch.setattr(cm, "enabled", lambda: "readwrite")

    class _App:
        def send_task(self, *a, **k):
            raise ConnectionError("broker down")

    import src.workers.celery_app as celery_module

    monkeypatch.setattr(celery_module, "celery_app", _App())
    assert job.enqueue_memory_extraction("s1") is False


def test_an_accepted_dispatch_carries_the_session_id(monkeypatch) -> None:
    monkeypatch.setattr(cm, "enabled", lambda: "readwrite")
    sent: list[tuple] = []

    class _App:
        def send_task(self, name, args=None, **k):
            sent.append((name, args))

    import src.workers.celery_app as celery_module

    monkeypatch.setattr(celery_module, "celery_app", _App())
    assert job.enqueue_memory_extraction("s1") is True
    assert sent == [(job.EXTRACT_TASK_NAME, [{"session_id": "s1"}])]


# ------------------------------------------------------------ score()


@pytest.fixture
def score(monkeypatch):
    """``database_operations()["score"]`` with grading and suggestions stubbed."""
    reports: list[dict | None] = [None, REPORT]  # first read: not graded yet; second: persisted

    async def get_eval(db, session_id):
        return reports.pop(0) if reports else REPORT

    graded: list[str] = []

    async def grade(db, session_id, engine=None):
        graded.append(session_id)

    monkeypatch.setattr(graph_runtime, "get_session_evaluation", get_eval)
    monkeypatch.setattr(graph_runtime, "evaluate_closed_session", grade)
    monkeypatch.setattr(graph_runtime, "enqueue_session_question_suggestions", lambda sid: True)
    monkeypatch.setattr(cm, "enabled", lambda: "readwrite")
    inline: list[str] = []

    async def inline_extract(db, session_id, *, evaluation, **_):
        inline.append(session_id)
        return {}

    monkeypatch.setattr(cm, "extract_after_session", inline_extract)
    ops = graph_runtime.database_operations(db=object(), user={"sub": "u1"}, session_row={"id": "s1", "user_id": "u1"})
    return ops["score"], graded, inline


@pytest.mark.asyncio
async def test_score_persists_the_report_then_dispatches_extraction(score, monkeypatch) -> None:
    run, graded, inline = score
    order: list[str] = []
    monkeypatch.setattr(graph_runtime, "evaluate_closed_session", _recording(order, "graded"))
    monkeypatch.setattr(job, "enqueue_memory_extraction", lambda sid: order.append("enqueued") or True)
    result = await run({})
    assert result == REPORT
    assert order == ["graded", "enqueued"], "the report is persisted before extraction is scheduled"
    assert inline == [], "an accepted dispatch means no inline extraction"


@pytest.mark.asyncio
async def test_score_falls_back_to_inline_extraction_when_dispatch_is_refused(score, monkeypatch) -> None:
    run, graded, inline = score
    monkeypatch.setattr(job, "enqueue_memory_extraction", lambda sid: False)
    result = await run({})
    assert result == REPORT, "the evaluation is returned whatever happened to extraction"
    assert inline == ["s1"]


@pytest.mark.asyncio
async def test_an_already_graded_session_is_not_dispatched_again(score, monkeypatch) -> None:
    run, graded, inline = score
    monkeypatch.setattr(graph_runtime, "get_session_evaluation", _const(REPORT))
    dispatched: list[str] = []
    monkeypatch.setattr(job, "enqueue_memory_extraction", lambda sid: dispatched.append(sid) or True)
    assert await run({}) == REPORT
    assert graded == [] and dispatched == [] and inline == []


def _recording(order: list[str], label: str):
    async def fn(*a, **k):
        order.append(label)

    return fn


def _const(value):
    async def fn(*a, **k):
        return value

    return fn
