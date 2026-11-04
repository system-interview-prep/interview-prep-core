from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from src.modules.matching.application.fit_history import (
    fit_counts,
    get_fit_attempt,
    list_candidate_fit_attempts,
    list_fit_attempts,
    list_latest_fits,
    save_latest_fit,
)


class _FakeDb:
    def __init__(self, rows=None) -> None:
        self.calls = []
        self.rows = rows or []
        self.committed = False

    async def execute(self, statement, params):
        self.calls.append((str(statement), params))
        mappings = SimpleNamespace(
            all=lambda: self.rows,
            first=lambda: self.rows[0] if self.rows else None,
        )
        return SimpleNamespace(mappings=lambda: mappings)

    async def commit(self):
        self.committed = True


def _result(statuses):
    result = SimpleNamespace(
        resume_id="cv-1",
        job_id="job-1",
        requirement_results=[SimpleNamespace(status=status) for status in statuses],
    )
    result.model_dump = lambda **_: {"resumeId": "cv-1", "jobId": "job-1"}
    return result


def test_fit_counts_excludes_not_applicable_requirements() -> None:
    assert fit_counts(_result(["met", "not_met", "unknown", "not_applicable"])) == (1, 3)


@pytest.mark.asyncio
async def test_save_latest_fit_appends_attempt_and_upserts_account_scoped_summary() -> None:
    db = _FakeDb()

    saved = await save_latest_fit(db, user_id="user-a", result=_result(["met", "not_met"]))

    assert isinstance(saved, str) and saved
    assert db.committed is True
    attempt_sql, attempt_params = db.calls[0]
    assert "INSERT INTO candidate_job_fit_attempts" in attempt_sql
    assert attempt_params["user_id"] == "user-a"
    assert attempt_params["job_id"] == "job-1"
    assert attempt_params["cv_id"] == "cv-1"
    assert attempt_params["met"] == 1
    assert attempt_params["total"] == 2
    assert attempt_params["id"]
    assert attempt_params["result_snapshot"] == '{"resumeId": "cv-1", "jobId": "job-1"}'
    summary_sql, params = db.calls[1]
    assert "ON CONFLICT (user_id, job_id) DO UPDATE" in summary_sql
    assert params == {
        "user_id": "user-a",
        "job_id": "job-1",
        "cv_id": "cv-1",
        "met": 1,
        "total": 2,
    }


@pytest.mark.asyncio
async def test_save_latest_fit_skips_empty_requirement_sets() -> None:
    db = _FakeDb()

    saved = await save_latest_fit(db, user_id="user-a", result=_result(["not_applicable"]))

    assert saved is None
    assert db.calls == []
    assert db.committed is False


@pytest.mark.asyncio
async def test_list_latest_fits_filters_by_user_and_maps_database_rows() -> None:
    analyzed_at = datetime(2026, 11, 3, 8, tzinfo=UTC)
    db = _FakeDb([{
        "job_id": "job-1",
        "cv_id": "cv-1",
        "cv_name": "Backend CV.pdf",
        "met": 2,
        "total": 8,
        "analyzed_at": analyzed_at,
        "attempt_id": "attempt-latest",
    }])

    fits = await list_latest_fits(db, user_id="user-a")

    assert db.calls[0][1] == {"user_id": "user-a"}
    assert "WHERE fit.user_id = :user_id" in db.calls[0][0]
    assert "attempt.cv_id = fit.cv_id" in db.calls[0][0]
    assert "ORDER BY attempt.analyzed_at DESC" in db.calls[0][0]
    assert fits == [{
        "jobId": "job-1",
        "cvId": "cv-1",
        "cvName": "Backend CV.pdf",
        "met": 2,
        "total": 8,
        "analyzedAt": analyzed_at.isoformat(),
        "attemptId": "attempt-latest",
    }]


@pytest.mark.asyncio
async def test_list_fit_attempts_is_scoped_to_user_and_job() -> None:
    analyzed_at = datetime(2026, 11, 3, 8, tzinfo=UTC)
    db = _FakeDb([{
        "id": "attempt-1",
        "job_id": "job-1",
        "cv_id": "cv-1",
        "cv_name": "Backend CV.pdf",
        "met": 2,
        "total": 8,
        "analyzed_at": analyzed_at,
        "has_snapshot": True,
    }])

    attempts = await list_fit_attempts(db, user_id="user-a", job_id="job-1")

    assert db.calls[0][1] == {"user_id": "user-a", "job_id": "job-1", "limit": 50}
    assert "attempt.user_id = :user_id AND attempt.job_id = :job_id" in db.calls[0][0]
    assert attempts == [{
        "id": "attempt-1",
        "jobId": "job-1",
        "cvId": "cv-1",
        "cvName": "Backend CV.pdf",
        "met": 2,
        "total": 8,
        "analyzedAt": analyzed_at.isoformat(),
        "hasSnapshot": True,
    }]


@pytest.mark.asyncio
async def test_list_candidate_fit_attempts_returns_recent_jobs_for_one_user() -> None:
    analyzed_at = datetime(2026, 11, 3, 8, tzinfo=UTC)
    db = _FakeDb([{
        "id": "attempt-1",
        "job_id": "job-1",
        "job_title": "OJT AI Engineer",
        "cv_id": "cv-1",
        "cv_name": "Backend CV.pdf",
        "met": 4,
        "total": 8,
        "analyzed_at": analyzed_at,
        "has_snapshot": True,
    }])

    attempts = await list_candidate_fit_attempts(db, user_id="user-a")

    sql, params = db.calls[0]
    assert params == {"user_id": "user-a", "limit": 100}
    assert "WHERE attempt.user_id = :user_id" in sql
    assert "ORDER BY attempt.analyzed_at DESC LIMIT :limit" in sql
    assert attempts == [{
        "id": "attempt-1",
        "jobId": "job-1",
        "jobTitle": "OJT AI Engineer",
        "cvId": "cv-1",
        "cvName": "Backend CV.pdf",
        "met": 4,
        "total": 8,
        "analyzedAt": analyzed_at.isoformat(),
        "hasSnapshot": True,
    }]


@pytest.mark.asyncio
async def test_get_fit_attempt_returns_snapshot_only_for_own_attempt() -> None:
    db = _FakeDb([{
        "id": "attempt-1",
        "job_id": "job-1",
        "cv_id": "cv-1",
        "cv_name": "Backend CV.pdf",
        "met": 2,
        "total": 8,
        "analyzed_at": datetime(2026, 11, 3, 8, tzinfo=UTC),
        "result_snapshot": {"fitBand": "partial_fit"},
    }])

    attempt = await get_fit_attempt(db, user_id="user-a", attempt_id="attempt-1")

    assert db.calls[0][1] == {"user_id": "user-a", "attempt_id": "attempt-1"}
    assert "attempt.user_id = :user_id AND attempt.id = :attempt_id" in db.calls[0][0]
    assert attempt == {
        "id": "attempt-1",
        "jobId": "job-1",
        "cvId": "cv-1",
        "cvName": "Backend CV.pdf",
        "met": 2,
        "total": 8,
        "analyzedAt": datetime(2026, 11, 3, 8, tzinfo=UTC).isoformat(),
        "resultSnapshot": {"fitBand": "partial_fit"},
    }
