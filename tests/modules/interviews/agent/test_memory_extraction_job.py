"""Post-session memory extraction as a durable background job (V2 spec §9).

The report is persisted first; the dispatch is recorded in PostgreSQL before
the task is sent; every state transition is one guarded SQL statement; the
job survives duplicate delivery (including the check-then-lock interleaving),
a lost worker, a refused or lost dispatch, concurrent reconcilers, and it
classifies its failures. Nothing here touches the grade.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from celery.exceptions import MaxRetriesExceededError

from src.modules.interviews.graph import runtime as graph_runtime
from src.modules.interviews.graph.checkpoints import MemoryStoreUnavailable
from src.modules.interviews.memory import candidate_memory as cm
from src.modules.interviews.memory import extraction_job as job

REPORT = {"overall_score": 7.5, "competency_scores": [{"name": "Redis", "score": 7.0}]}
STORE = object()  # the job never touches the store itself; extraction does


class _Rows:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows


class _DB:
    """PostgreSQL as the job sees it: the job state, the advisory locks, the
    guarded transition and the reconciler's query. The transition enforces the
    same guards as the real statement, so a test can prove a write was refused.
    Every call is logged in order."""

    def __init__(
        self,
        *,
        lock: bool = True,
        sweep_lock: bool = True,
        states: dict[str, dict | None] | None = None,
        on_lock=None,
        on_rollback=None,
    ) -> None:
        self.lock = lock
        self.sweep_lock = sweep_lock
        self.states: dict[str, dict | None] = dict(states or {})
        self.on_lock = on_lock
        self.on_rollback = on_rollback
        self.log: list[Any] = []
        self.rows: list[dict] = []

    async def scalar(self, statement: Any, params: dict | None = None):
        sql = str(statement)
        params = params or {}
        if "pg_try_advisory_xact_lock" in sql:
            if params["key"] == job.RECONCILE_LOCK_KEY:
                self.log.append("sweep_lock")
                return self.sweep_lock
            self.log.append("lock")
            if self.on_lock is not None:
                self.on_lock(self)
            return self.lock
        if sql.strip().startswith("SELECT metadata->'memory_extraction'"):
            self.log.append("read")
            state = self.states.get(str(params["sid"]))
            return json.dumps(state) if state is not None else None
        if "UPDATE interview_sessions" in sql and "RETURNING" in sql:
            sid = str(params["sid"])
            current = self.states.get(sid) or {}
            if "<> 'DONE'" in sql and current.get("status") == "DONE":
                self.log.append(("refused", "done"))
                return None
            if "'permanent'" in sql and "<> 'true'" in sql and current.get("permanent") is True:
                self.log.append(("refused", "permanent"))
                return None
            if ":seen" in sql and str(current.get("updated_at") or "") != params["seen"]:
                self.log.append(("refused", "seen"))
                return None
            merged = {**current, **json.loads(params["job"]), "dispatches": int(current.get("dispatches") or 0) + int(params["bump"])}
            self.states[sid] = merged
            self.log.append(("write", merged["status"]))
            return json.dumps(merged)
        raise AssertionError(sql)

    async def execute(self, statement: Any, params: dict | None = None):
        sql = str(statement)
        if "FROM interview_evaluations e" in sql:
            self.log.append("reconcile_query")
            return _Rows(self.rows)
        raise AssertionError(sql)

    async def commit(self) -> None:
        self.log.append("commit")

    async def rollback(self) -> None:
        self.log.append("rollback")
        if self.on_rollback is not None:
            self.on_rollback(self)


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
    """The langmem run replaced by a recorder that logs itself on the db."""
    calls: list[dict] = []

    async def fake(db, session_id, *, evaluation, store=None, raise_errors=False, **_):
        db.log.append("extract")
        calls.append({"session_id": session_id, "evaluation": evaluation, "store": store, "raise_errors": raise_errors})
        return {"inserted": 1, "patched": 0, "removed": 0, "truncated": 0}

    monkeypatch.setattr(job, "extract_after_session", fake)
    return calls


async def _run(db, **kw):
    kw.setdefault("store", STORE)
    kw.setdefault("evaluation", REPORT)
    return await job.run_memory_extraction_job(db, "s1", **kw)


DONE = {"status": "DONE", "counts": {"inserted": 2}, "updated_at": "2026-11-26T00:00:00+00:00"}
PERMANENT = {"status": "FAILED", "permanent": True, "reason": "missing_dependency", "updated_at": "2026-11-26T00:00:00+00:00"}


# ---------------------------------------------------------------- the job


@pytest.mark.asyncio
async def test_a_first_delivery_extracts_under_the_lock_and_records_done(readwrite, extraction) -> None:
    db = _DB()
    result = await _run(db)
    assert result["status"] == "COMPLETE"
    assert extraction[0]["raise_errors"] is True, "the worker must see failures to retry them"
    assert db.states["s1"]["status"] == "DONE" and db.states["s1"]["counts"]["inserted"] == 1
    # Order: cheap read, lock, authoritative read, PROCESSING, extraction, DONE, commit.
    assert db.log == ["read", "lock", "read", ("write", "PROCESSING"), "extract", ("write", "DONE"), "commit"]


@pytest.mark.asyncio
async def test_the_advisory_lock_is_held_throughout_the_extraction(readwrite, extraction) -> None:
    """A transaction-scoped lock is released by commit or rollback. Neither may
    happen between taking it and writing DONE; no write precedes the lock."""
    db = _DB()
    await _run(db)
    before_lock = db.log[: db.log.index("lock")]
    assert not any(isinstance(item, tuple) and item[0] == "write" for item in before_lock)
    after_lock = db.log[db.log.index("lock") :]
    assert "commit" not in after_lock[: after_lock.index(("write", "DONE"))]
    assert "rollback" not in after_lock


@pytest.mark.asyncio
async def test_a_duplicate_delivery_stops_before_the_lock(readwrite, extraction) -> None:
    db = _DB(states={"s1": DONE})
    again = await _run(db)
    assert again["status"] == "SKIPPED" and again["reason"] == "already_extracted" and again["counts"] == {"inserted": 2}
    assert "lock" not in db.log and extraction == []


@pytest.mark.asyncio
async def test_check_then_lock_race_is_caught_by_the_read_under_the_lock(readwrite, extraction) -> None:
    """Two deliveries both read 'not done', then serialize on the lock. The
    one that locks second must re-read and find what the first wrote."""

    def other_worker_finishes_first(db: _DB) -> None:
        db.states["s1"] = dict(DONE)

    db = _DB(on_lock=other_worker_finishes_first)
    result = await _run(db)
    assert result["status"] == "SKIPPED" and result["reason"] == "already_extracted"
    assert extraction == [], "the second worker must not extract again"
    assert db.log == ["read", "lock", "read", "rollback"]
    assert any(name == "candidate_memory_job_duplicate" and kw.get("after_lock") for name, kw in readwrite)


@pytest.mark.asyncio
async def test_a_permanent_failure_is_rechecked_under_the_lock(readwrite, extraction) -> None:
    def other_worker_gave_up(db: _DB) -> None:
        db.states["s1"] = dict(PERMANENT)

    db = _DB(on_lock=other_worker_gave_up)
    result = await _run(db)
    assert result["status"] == "FAILED" and result["permanent"] is True and result["skipped"] is True
    assert extraction == [] and db.log == ["read", "lock", "read", "rollback"]


@pytest.mark.asyncio
async def test_a_session_held_by_another_worker_is_retried_not_failed(readwrite, extraction) -> None:
    db = _DB(lock=False)
    result = await _run(db)
    assert result["status"] == "RETRY" and result["retry_in"] > 0
    assert extraction == [] and "s1" not in db.states
    assert db.log[-1] == "rollback"


@pytest.mark.asyncio
async def test_a_lost_worker_leaves_processing_which_the_redelivery_resumes(readwrite, extraction) -> None:
    db = _DB(states={"s1": {"status": "PROCESSING", "attempt": 1}})
    result = await _run(db, attempt=2)
    assert result["status"] == "COMPLETE"
    assert db.states["s1"]["status"] == "DONE" and db.states["s1"]["attempt"] == 2
    assert len(extraction) == 1


# ------------------------------------------------------- guarded transitions


@pytest.mark.asyncio
async def test_a_failure_recorded_after_rollback_never_overwrites_a_done_written_meanwhile(readwrite, monkeypatch) -> None:
    """The extraction raised; its rollback released the lock; another delivery
    took it and finished. The FAILED write must be refused, not win."""

    async def boom(db, *a, **k):
        db.log.append("extract")
        raise ConnectionError("provider down")

    monkeypatch.setattr(job, "extract_after_session", boom)

    def other_delivery_finishes(db: _DB) -> None:
        db.states["s1"] = dict(DONE)

    db = _DB(on_rollback=other_delivery_finishes)
    result = await _run(db, attempt=1, max_attempts=3)
    assert result["status"] == "SKIPPED" and result["reason"] == "already_extracted"
    assert db.states["s1"]["status"] == "DONE", "DONE stands"
    assert ("refused", "done") in db.log
    assert not any(name == "candidate_memory_job_failed" for name, _ in readwrite)


@pytest.mark.asyncio
async def test_queued_never_overwrites_done_or_revives_a_permanent_failure() -> None:
    done = _DB(states={"s1": dict(DONE)})
    assert await job.mark_queued(done, "s1") is None and done.states["s1"]["status"] == "DONE"
    permanent = _DB(states={"s1": dict(PERMANENT)})
    assert await job.mark_queued(permanent, "s1") is None and permanent.states["s1"]["status"] == "FAILED"


@pytest.mark.asyncio
async def test_a_failure_write_is_not_computed_from_stale_state(readwrite, monkeypatch) -> None:
    """Between PROCESSING and the failure, a concurrent mark_queued bumped
    dispatches. The FAILED transition merges in SQL, so the bump survives."""

    async def boom(db, *a, **k):
        db.states["s1"] = {**db.states["s1"], "dispatches": 4, "updated_at": "later"}
        raise ConnectionError("provider down")

    monkeypatch.setattr(job, "extract_after_session", boom)
    db = _DB()
    with pytest.raises(job.TransientExtractionError):
        await _run(db, attempt=1, max_attempts=3)
    assert db.states["s1"]["status"] == "FAILED" and db.states["s1"]["dispatches"] == 4


@pytest.mark.asyncio
async def test_mark_queued_is_one_guarded_statement_that_increments_in_sql() -> None:
    db = _DB()
    state = await job.mark_queued(db, "s1")
    assert state["status"] == "QUEUED" and state["dispatches"] == 1
    assert db.log == [("write", "QUEUED"), "commit"], "no read-modify-write"
    assert (await job.mark_queued(db, "s1"))["dispatches"] == 2


# ------------------------------------------------------------ failure classes


@pytest.mark.asyncio
async def test_a_transient_extraction_failure_is_recorded_and_raised_for_retry(readwrite, monkeypatch) -> None:
    async def boom(db, *a, **k):
        db.log.append("extract")
        raise ConnectionError("provider down")

    monkeypatch.setattr(job, "extract_after_session", boom)
    db = _DB()
    with pytest.raises(job.TransientExtractionError):
        await _run(db, attempt=1, max_attempts=3)
    state = db.states["s1"]
    assert state["status"] == "FAILED" and state["permanent"] is False and state["reason"] == "transient"
    assert "provider down" in state["error"]
    assert db.log[db.log.index("extract") :] == ["extract", "rollback", ("write", "FAILED"), "commit"]


@pytest.mark.asyncio
async def test_the_last_allowed_attempt_turns_a_transient_failure_permanent(readwrite, monkeypatch) -> None:
    async def boom(*a, **k):
        raise ConnectionError("still down")

    monkeypatch.setattr(job, "extract_after_session", boom)
    db = _DB()
    result = await _run(db, attempt=3, max_attempts=3)
    assert result["status"] == "FAILED" and result["permanent"] is True and result["reason"] == "attempts_exhausted"
    assert db.states["s1"]["permanent"] is True


@pytest.mark.asyncio
async def test_a_refused_provider_request_is_permanent_on_the_first_attempt(readwrite, monkeypatch) -> None:
    async def refused(*a, **k):
        raise type("OpenAIAuthenticationError", (Exception,), {})("401 incorrect api key")

    monkeypatch.setattr(job, "extract_after_session", refused)
    result = await _run(_DB(), attempt=1, max_attempts=3)
    assert result["status"] == "FAILED" and result["permanent"] is True and result["reason"] == "rejected"


@pytest.mark.asyncio
async def test_a_missing_dependency_is_recorded_under_the_lock_and_not_retried(readwrite, extraction) -> None:
    db = _DB()
    result = await _run(db, store=None, store_error=MemoryStoreUnavailable("missing dependency: langmem", permanent=True))
    assert result["status"] == "FAILED" and result["permanent"] is True and result["reason"] == "missing_dependency"
    assert db.log == ["read", "lock", "read", ("write", "FAILED"), "commit"] and extraction == []


@pytest.mark.asyncio
async def test_a_store_connection_failure_is_transient_never_a_successful_skip(readwrite, extraction) -> None:
    db = _DB()
    with pytest.raises(job.TransientExtractionError):
        await _run(db, store=None, store_error=MemoryStoreUnavailable("cannot connect", permanent=False))
    assert db.states["s1"]["status"] == "FAILED" and db.states["s1"]["permanent"] is False
    assert db.log.index("lock") < db.log.index(("write", "FAILED"))
    db2 = _DB()
    with pytest.raises(job.TransientExtractionError):
        await _run(db2, store=None)
    assert extraction == []


@pytest.mark.asyncio
async def test_a_store_failure_yields_to_a_done_that_appeared_under_the_lock(readwrite, extraction) -> None:
    def other_worker_finishes_first(db: _DB) -> None:
        db.states["s1"] = dict(DONE)

    db = _DB(on_lock=other_worker_finishes_first)
    result = await _run(db, store=None, store_error=MemoryStoreUnavailable("cannot connect", permanent=False))
    assert result["status"] == "SKIPPED" and result["reason"] == "already_extracted"
    assert db.states["s1"]["status"] == "DONE" and not any(isinstance(i, tuple) and i[0] == "write" for i in db.log)


@pytest.mark.asyncio
async def test_a_permanently_failed_session_is_not_run_again(readwrite, extraction) -> None:
    db = _DB(states={"s1": dict(PERMANENT)})
    result = await _run(db)
    assert result["status"] == "FAILED" and result["permanent"] is True and result["skipped"] is True
    assert extraction == [] and "lock" not in db.log


@pytest.mark.asyncio
async def test_nothing_to_extract_from_is_a_skip(readwrite, extraction) -> None:
    result = await _run(_DB(), evaluation=None)
    assert result["status"] == "SKIPPED" and result["reason"] == "evaluation_not_available"
    assert extraction == []


@pytest.mark.asyncio
async def test_memory_off_means_no_job_and_no_dispatch(monkeypatch, extraction) -> None:
    monkeypatch.setattr(cm, "enabled", lambda: "read")
    result = await _run(_DB())
    assert result["reason"] == "memory_disabled"
    assert job.enqueue_memory_extraction("s1") is False
    assert extraction == []


@pytest.mark.parametrize(
    "exc,kind",
    [
        (ImportError("No module named langmem"), "permanent"),
        (MemoryStoreUnavailable("x", permanent=True), "permanent"),
        (MemoryStoreUnavailable("x", permanent=False), "transient"),
        (job.PermanentExtractionError("bad"), "permanent"),
        (ConnectionError("refused"), "transient"),
        (TimeoutError(), "transient"),
        (type("OperationalError", (Exception,), {})("db gone"), "transient"),
        (type("RateLimitError", (Exception,), {})("429"), "transient"),
        (type("OpenAIAuthenticationError", (Exception,), {})("401"), "permanent"),  # seen on the real broker
        (type("BadRequestError", (Exception,), {})("400"), "permanent"),
        (ValueError("unknown"), "transient"),  # bounded by the attempt cap
    ],
)
def test_failures_are_classified(exc, kind) -> None:
    assert job.classify(exc) == kind


def test_retry_backoff_is_exponential_and_bounded() -> None:
    no_jitter = lambda: 0.0  # noqa: E731
    assert [job.retry_delay_seconds(a, jitter=no_jitter) for a in (1, 2, 3, 4)] == [30, 60, 120, 240]
    assert job.retry_delay_seconds(20, jitter=no_jitter) == job.BACKOFF_MAX_SECONDS
    assert job.retry_delay_seconds(20, jitter=lambda: 1.0) <= job.BACKOFF_MAX_SECONDS + 30


# ------------------------------------------------------------------ dispatch


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


# ------------------------------------------------------------ reconciliation


def _state(status: str, *, age_seconds: float, dispatches: int = 1, **extra) -> dict:
    return {
        "status": status,
        "dispatches": dispatches,
        "updated_at": (datetime.now(UTC) - timedelta(seconds=age_seconds)).isoformat(),
        **extra,
    }


def _sweep_db(**states) -> _DB:
    db = _DB(states=states)
    db.rows = [{"session_id": sid, "job": state} for sid, state in states.items()]
    return db


@pytest.mark.asyncio
async def test_the_reconciler_redispatches_lost_and_stale_jobs_but_not_fresh_ones(readwrite) -> None:
    db = _sweep_db(
        lost=None,  # evaluation persisted, process died before send_task
        fresh=_state("QUEUED", age_seconds=30),
        stale=_state("PROCESSING", age_seconds=3600),
        transient=_state("FAILED", age_seconds=1200, permanent=False, dispatches=2),
        worn=_state("FAILED", age_seconds=1200, permanent=False, dispatches=5),
    )
    sent: list[str] = []
    summary = await job.reconcile_memory_extraction(db, enqueue=lambda sid: sent.append(sid) or True)
    assert sorted(sent) == ["lost", "stale", "transient"]
    assert summary == {"status": "COMPLETE", "candidates": 5, "dispatched": 3, "fresh": 1, "exhausted": 1, "dispatch_failed": 0, "raced": 0}
    assert db.states["lost"]["status"] == "QUEUED" and db.states["lost"]["dispatches"] == 1
    assert db.states["transient"]["dispatches"] == 3
    assert db.states["worn"]["status"] == "FAILED" and db.states["worn"]["permanent"] is True
    assert db.states["worn"]["reason"] == "dispatch_exhausted"
    assert db.log[0] == "sweep_lock" and db.log[-1] == "commit"


@pytest.mark.asyncio
async def test_only_one_replica_sweeps_at_a_time(readwrite) -> None:
    db = _sweep_db(lost=None)
    db.sweep_lock = False
    summary = await job.reconcile_memory_extraction(db, enqueue=lambda sid: True)
    assert summary == {"status": "SKIPPED", "reason": "sweep_in_progress"}
    assert db.log == ["sweep_lock", "rollback"] and db.states["lost"] is None


@pytest.mark.asyncio
async def test_a_worker_that_moves_first_wins_over_the_sweep(readwrite) -> None:
    """Between the sweep's read and its QUEUED write the worker finished the
    job (DONE) -- the compare-and-set write is refused and DONE stands."""
    db = _sweep_db(stale=_state("PROCESSING", age_seconds=3600))

    def worker_finishes_during_dispatch(sid: str) -> bool:
        db.states[sid] = dict(DONE)
        return True

    summary = await job.reconcile_memory_extraction(db, enqueue=worker_finishes_during_dispatch)
    assert summary["raced"] == 1 and summary["dispatched"] == 0
    assert db.states["stale"]["status"] == "DONE"


@pytest.mark.asyncio
async def test_the_reconciler_reports_a_broker_that_is_still_down(readwrite) -> None:
    db = _sweep_db(lost=None)
    summary = await job.reconcile_memory_extraction(db, enqueue=lambda sid: False)
    assert summary["dispatch_failed"] == 1 and summary["dispatched"] == 0
    assert db.states["lost"] is None, "no QUEUED record for a dispatch that did not happen"


@pytest.mark.asyncio
async def test_a_dry_run_reports_without_locking_writing_or_dispatching(readwrite) -> None:
    db = _sweep_db(lost=None, worn=_state("FAILED", age_seconds=1200, permanent=False, dispatches=5))
    sent: list[str] = []
    summary = await job.reconcile_memory_extraction(db, enqueue=lambda sid: sent.append(sid) or True, dry_run=True)
    assert summary["status"] == "DRY_RUN" and summary["dispatched"] == 1 and summary["exhausted"] == 1
    assert summary["planned"] == [
        {"session_id": "lost", "action": "dispatch", "dispatches": 1},
        {"session_id": "worn", "action": "exhaust", "dispatches": 5},
    ]
    assert sent == [] and db.log == ["reconcile_query"]
    assert db.states["lost"] is None and db.states["worn"]["permanent"] is False


def test_eligibility_is_least_recently_attempted_first_inside_a_bounded_window() -> None:
    sql = job._RECONCILE_SQL
    assert "make_interval(hours => :hours)" in sql and "make_interval(secs => :grace)" in sql
    assert "<> 'DONE'" in sql and "'permanent'" in sql
    assert re.search(r"ORDER BY COALESCE\(\(s\.metadata->'memory_extraction'->>'updated_at'\)::timestamptz, e\.created_at\) ASC", sql)
    assert "DESC" not in sql, "newest-first would starve the oldest of a backlog wider than the limit"
    assert job.RECONCILE_WINDOW_HOURS == 6 and job.RECONCILE_LIMIT == 20


@pytest.mark.asyncio
async def test_the_reconciler_does_nothing_when_memory_is_off(monkeypatch) -> None:
    monkeypatch.setattr(cm, "enabled", lambda: "read")
    db = _DB()
    assert (await job.reconcile_memory_extraction(db))["status"] == "SKIPPED"
    assert db.log == []


# ------------------------------------------------------ manual recovery tool

_OPS = Path(__file__).resolve().parents[4] / "scripts" / "ops" / "reconcile_memory_extraction.py"


def _ops_module():
    spec = importlib.util.spec_from_file_location("reconcile_memory_extraction_cli", _OPS)
    module = importlib.util.module_from_spec(spec)
    sys.modules["reconcile_memory_extraction_cli"] = module
    spec.loader.exec_module(module)
    return module


def test_the_manual_recovery_tool_is_bounded_and_defaults_to_the_automatic_window() -> None:
    ops = _ops_module()
    args = ops.parse_args([])
    assert (args.window_hours, args.limit, args.max_dispatches, args.dry_run) == (6, 20, 5, False)
    assert ops.parse_args(["--window-hours", "48", "--limit", "50", "--dry-run"]).dry_run is True
    with pytest.raises(SystemExit):
        ops.parse_args(["--window-hours", "721"])
    with pytest.raises(SystemExit):
        ops.parse_args(["--limit", "201"])


@pytest.mark.asyncio
async def test_the_manual_recovery_tool_passes_its_bounds_to_the_reconciler(monkeypatch) -> None:
    ops = _ops_module()
    seen: dict = {}

    async def fake_reconcile(db, **kwargs):
        seen.update(kwargs)
        return {"status": "DRY_RUN"}

    class _Factory:
        def __call__(self):
            return self

        async def __aenter__(self):
            return object()

        async def __aexit__(self, *a):
            return False

    import src.infrastructure.database as database

    monkeypatch.setattr(database, "StandaloneSessionFactory", _Factory())
    monkeypatch.setattr(job, "reconcile_memory_extraction", fake_reconcile)
    summary = await ops.run(ops.parse_args(["--window-hours", "48", "--limit", "7", "--max-dispatches", "2", "--dry-run"]))
    assert summary == {"status": "DRY_RUN"}
    assert seen == {"limit": 7, "window_hours": 48, "max_dispatches": 2, "dry_run": True}


# ------------------------------------------------------------------- score()


@pytest.fixture
def score(monkeypatch):
    """``database_operations()["score"]`` with grading and suggestions stubbed."""
    reports: list[dict | None] = [None, REPORT]

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
    db = _DB()
    ops = graph_runtime.database_operations(db=db, user={"sub": "u1"}, session_row={"id": "s1", "user_id": "u1"})
    return ops["score"], graded, inline, db


@pytest.mark.asyncio
async def test_score_persists_the_report_records_the_dispatch_then_sends(score, monkeypatch) -> None:
    run, graded, inline, db = score
    order: list[str] = []

    async def grade(*a, **k):
        order.append("graded")

    monkeypatch.setattr(graph_runtime, "evaluate_closed_session", grade)
    monkeypatch.setattr(job, "enqueue_memory_extraction", lambda sid: order.append("sent") or True)
    result = await run({})
    assert result == REPORT
    assert order == ["graded", "sent"]
    assert db.states["s1"]["status"] == "QUEUED", "the dispatch is durable before the task is sent"
    assert inline == []


@pytest.mark.asyncio
async def test_score_falls_back_to_inline_extraction_when_dispatch_is_refused(score, monkeypatch) -> None:
    run, graded, inline, db = score
    monkeypatch.setattr(job, "enqueue_memory_extraction", lambda sid: False)
    assert await run({}) == REPORT
    assert inline == ["s1"]


@pytest.mark.asyncio
async def test_an_already_graded_session_is_not_dispatched_again(score, monkeypatch) -> None:
    run, graded, inline, db = score

    async def const(*a, **k):
        return REPORT

    monkeypatch.setattr(graph_runtime, "get_session_evaluation", const)
    dispatched: list[str] = []
    monkeypatch.setattr(job, "enqueue_memory_extraction", lambda sid: dispatched.append(sid) or True)
    assert await run({}) == REPORT
    assert graded == [] and dispatched == [] and inline == [] and db.log == []


# ------------------------------------------------------------ the Celery task


@pytest.fixture
def task(monkeypatch):
    from src.workers.tasks import interview_memory as module

    retries: list[dict] = []

    class _Retry(Exception):
        pass

    def fake_retry(**kwargs):
        retries.append(kwargs)
        raise _Retry()

    monkeypatch.setattr(module.extract_session_memory, "retry", fake_retry)
    return module, retries, _Retry


def _runs(monkeypatch, module, outcomes: list):
    """``extract_session_memory_async`` replaced by a script of outcomes."""
    seen: list[int] = []

    async def fake(session_id, *, attempt):
        seen.append(attempt)
        outcome = outcomes[len(seen) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(module, "extract_session_memory_async", fake)
    return seen


def test_the_task_retries_a_transient_failure_with_the_bounded_backoff(task, monkeypatch) -> None:
    module, retries, Retry = task
    seen = _runs(monkeypatch, module, [job.TransientExtractionError("down")])
    module.extract_session_memory.push_request(retries=1)
    try:
        with pytest.raises(Retry):
            module.extract_session_memory({"session_id": "s1"})
    finally:
        module.extract_session_memory.pop_request()
    assert seen == [2], "attempt = retries + 1"
    assert 60 <= retries[0]["countdown"] <= 90


def test_the_task_retries_a_busy_session_later(task, monkeypatch) -> None:
    module, retries, Retry = task
    _runs(monkeypatch, module, [{"status": "RETRY", "retry_in": 45}])
    with pytest.raises(Retry):
        module.extract_session_memory({"session_id": "s1"})
    assert retries[0]["countdown"] == 45


def test_a_permanent_failure_ends_the_task_without_retry(task, monkeypatch) -> None:
    module, retries, _ = task
    _runs(monkeypatch, module, [{"status": "FAILED", "permanent": True, "reason": "missing_dependency"}])
    result = module.extract_session_memory({"session_id": "s1"})
    assert result["permanent"] is True and retries == []


def test_the_task_is_bounded_by_the_configured_attempts(task, monkeypatch) -> None:
    module, retries, _ = task
    assert module.extract_session_memory.max_retries == job.max_attempts() - 1
    _runs(monkeypatch, module, [job.TransientExtractionError("down")])

    def exhausted(**kwargs):
        raise MaxRetriesExceededError()

    monkeypatch.setattr(module.extract_session_memory, "retry", exhausted)
    result = module.extract_session_memory({"session_id": "s1"})
    assert result == {"sessionId": "s1", "status": "FAILED", "permanent": True, "reason": "attempts_exhausted"}


def test_a_missing_session_id_is_skipped(task) -> None:
    module, _, _ = task
    assert module.extract_session_memory({})["reason"] == "missing_session_id"
