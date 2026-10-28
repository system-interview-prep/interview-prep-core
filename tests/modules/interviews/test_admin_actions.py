"""Admin interventions: close a stuck session, grade a closed one without a report."""

import asyncio
import importlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

actions = importlib.import_module("src.modules.interviews.application.admin_actions")


class _Db:
    def __init__(self, *, session=None, evaluated=False, job=None, updated=1, gradable=True) -> None:
        self.session = session
        self.evaluated = evaluated
        self.gradable = gradable
        self.job = job
        self.updated = updated
        self.statements: list[tuple[str, dict]] = []
        self.commits = 0

    async def execute(self, statement, params):
        sql = str(statement)
        self.statements.append((sql, params))
        if sql.startswith("UPDATE interview_sessions"):
            return SimpleNamespace(rowcount=self.updated)
        row = self.job if "interview_evaluation_jobs" in sql else self.session
        return SimpleNamespace(mappings=lambda: SimpleNamespace(one_or_none=lambda: row))

    async def scalar(self, statement, params):
        if "interview_turns" in str(statement):
            return 1 if self.gradable else None
        return 1 if self.evaluated else None

    async def commit(self) -> None:
        self.commits += 1


def _session(status="ACTIVE", end_reason=None) -> dict:
    return {"id": "ses-1", "status": status, "end_reason": end_reason}


async def test_close_refuses_unknown_and_closed_sessions() -> None:
    with pytest.raises(actions.AdminActionError) as missing:
        await actions.close_session(_Db(), "ses-x")
    assert missing.value.status_code == 404

    with pytest.raises(actions.AdminActionError) as closed:
        await actions.close_session(_Db(session=_session("CLOSED")), "ses-1")
    assert (closed.value.status_code, closed.value.code) == (409, "already_closed")


async def test_close_uses_an_allowed_end_reason_and_keeps_an_existing_one() -> None:
    db = _Db(session=_session("ACTIVE"))
    result = await actions.close_session(db, "ses-1", note="ứng viên rời phòng")

    update_sql, params = db.statements[-1]
    assert "COALESCE(end_reason, :reason)" in update_sql and "status <> 'CLOSED'" in update_sql
    assert params["reason"] == "TECHNICAL_FAILURE"
    assert result == {"previousStatus": "ACTIVE", "endReason": "TECHNICAL_FAILURE"}
    assert db.commits == 1

    kept = await actions.close_session(_Db(session=_session("ACTIVE", "HARD_TIMEOUT")), "ses-1")
    assert kept["endReason"] == "HARD_TIMEOUT"


async def test_close_reports_a_race_with_the_runtime() -> None:
    with pytest.raises(actions.AdminActionError) as raced:
        await actions.close_session(_Db(session=_session("ACTIVE"), updated=0), "ses-1")
    assert raced.value.code == "already_closed"


@pytest.mark.parametrize(
    ("db", "code"),
    [
        (_Db(session=_session("ACTIVE")), "not_closed"),
        (_Db(session=_session("CLOSED"), evaluated=True), "already_graded"),
        (
            _Db(
                session=_session("CLOSED"),
                job={"status": "PROCESSING", "lease_until": datetime.now(UTC) + timedelta(minutes=5)},
            ),
            "running",
        ),
        # Only the greeting was answered: nothing for the grader to score.
        (_Db(session=_session("CLOSED"), gradable=False), "no_answers"),
    ],
)
async def test_grading_is_refused(db, code) -> None:
    assert await actions.grading_state(db, "ses-1") == {"allowed": False, "code": code}
    with pytest.raises(actions.AdminActionError) as refused:
        await actions.start_grading(db, "ses-1")
    assert refused.value.code == code


@pytest.mark.parametrize(
    "job",
    [
        None,
        {"status": "FAILED", "lease_until": datetime.now(UTC)},
        # A lease that ran out means the grader died mid-run.
        {"status": "PROCESSING", "lease_until": datetime.now(UTC) - timedelta(minutes=1)},
    ],
)
async def test_grading_is_allowed_after_failure_or_expired_lease(job) -> None:
    db = _Db(session=_session("CLOSED"), job=job)
    assert await actions.grading_state(db, "ses-1") == {"allowed": True, "code": None}


class _FactoryDb:
    async def __aenter__(self):
        return "db"

    async def __aexit__(self, *exc) -> None:
        return None


async def test_start_grading_runs_in_the_background(monkeypatch) -> None:
    graded: list[str] = []
    gate = asyncio.Event()

    async def _evaluate(db, session_id):
        await gate.wait()
        graded.append(session_id)

    monkeypatch.setattr(actions, "evaluate_closed_session", _evaluate)
    monkeypatch.setattr(actions, "SessionFactory", _FactoryDb)
    db = _Db(session=_session("CLOSED"))

    assert await actions.start_grading(db, "ses-1") == {"started": True}
    # A second click while the first run is going is refused.
    assert (await actions.grading_state(db, "ses-1"))["code"] == "running"

    task = actions._running["ses-1"]
    gate.set()
    await task
    assert graded == ["ses-1"]
    assert not actions.grading_running("ses-1")


async def test_background_failure_is_reported_not_raised(monkeypatch) -> None:
    events: list[str] = []

    async def _boom(db, session_id):
        raise RuntimeError("grader down")

    monkeypatch.setattr(actions, "evaluate_closed_session", _boom)
    monkeypatch.setattr(actions, "SessionFactory", _FactoryDb)
    monkeypatch.setattr(actions, "trace_event", lambda workflow, event, **_: events.append(event))

    await actions.start_grading(_Db(session=_session("CLOSED")), "ses-2")
    await actions._running["ses-2"]
    assert events == ["evaluation_admin_requested", "evaluation_admin_failed"]


class _PendingDb:
    def __init__(self, ids):
        self.ids = ids
        self.sql = ""
        self.params = {}

    async def execute(self, statement, params):
        self.sql, self.params = str(statement), params
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: self.ids))


async def test_pending_sessions_filter_by_user_and_skip_ones_being_graded(monkeypatch) -> None:
    monkeypatch.setattr(actions, "grading_running", lambda sid: sid == "s2")
    db = _PendingDb(["s1", "s2", "s3"])
    assert await actions.pending_sessions(db, user_id="u1") == ["s1", "s3"]
    assert "s.user_id = :uid" in db.sql and db.params["uid"] == "u1"
    assert "'ANSWERED'" in db.sql and "NOT EXISTS (SELECT 1 FROM interview_evaluations" in db.sql
    await actions.pending_sessions(db)
    assert ":uid" not in db.sql


async def test_batch_grades_one_after_another_and_counts(monkeypatch) -> None:
    order: list[str] = []

    async def _fake_grade(session_id):
        order.append(session_id)
        actions._running.pop(session_id, None)
        return session_id != "bad"

    monkeypatch.setattr(actions, "_grade", _fake_grade)
    monkeypatch.setattr(actions, "trace_event", lambda *a, **k: None)
    actions._batches.clear()
    db = _PendingDb(["s1", "bad", "s3"])

    result = await actions.start_batch_grading(db, user_id="u9")
    assert result["queued"] == 3
    with pytest.raises(actions.AdminActionError) as busy:
        await actions.start_batch_grading(db, user_id="u9")
    assert busy.value.code == "batch_running"

    for _ in range(50):
        if not actions.batch_state("u9")["running"]:
            break
        await asyncio.sleep(0.01)
    state = actions.batch_state("u9")
    assert order == ["s1", "bad", "s3"]
    assert (state["graded"], state["failed"], state["running"]) == (2, 1, False)


async def test_batch_with_nothing_to_grade_is_refused() -> None:
    actions._batches.clear()
    with pytest.raises(actions.AdminActionError) as empty:
        await actions.start_batch_grading(_PendingDb([]), user_id=None)
    assert empty.value.code == "nothing_to_grade"


def test_gradable_turn_rule_is_shared_by_single_and_batch_grading() -> None:
    single = actions._GRADABLE_TURN_SQL
    batch = actions._pending_sql(None)
    for sql in (single, batch):
        assert "'ANSWERED'" in sql and "('', 'WARM_UP', 'CLOSING')" in sql
    assert "t.session_id = :sid" in single and "t.session_id = s.id" in batch


class _CountDb:
    def __init__(self) -> None:
        self.sql = ""
        self.params: dict = {}

    async def scalar(self, statement, params):
        self.sql, self.params = str(statement), params
        return 7


async def test_count_pending_counts_every_gradable_session_without_the_limit() -> None:
    db = _CountDb()
    assert await actions.count_pending(db, user_id="u1") == 7
    assert db.sql.startswith("SELECT COUNT(*) FROM (") and "LIMIT" not in db.sql and "ORDER BY" not in db.sql
    assert db.params == {"uid": "u1"}
    await actions.count_pending(db)
    assert db.params == {} and ":uid" not in db.sql
