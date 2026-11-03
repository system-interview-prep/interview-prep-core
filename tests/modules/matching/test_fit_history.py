from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from src.modules.matching.application.fit_history import fit_counts, list_latest_fits, save_latest_fit


class _FakeDb:
    def __init__(self, rows=None) -> None:
        self.calls = []
        self.rows = rows or []
        self.committed = False

    async def execute(self, statement, params):
        self.calls.append((str(statement), params))
        return SimpleNamespace(mappings=lambda: SimpleNamespace(all=lambda: self.rows))

    async def commit(self):
        self.committed = True


def _result(statuses):
    return SimpleNamespace(
        resume_id="cv-1",
        job_id="job-1",
        requirement_results=[SimpleNamespace(status=status) for status in statuses],
    )


def test_fit_counts_excludes_not_applicable_requirements() -> None:
    assert fit_counts(_result(["met", "not_met", "unknown", "not_applicable"])) == (1, 3)


@pytest.mark.asyncio
async def test_save_latest_fit_upserts_only_account_scoped_summary() -> None:
    db = _FakeDb()

    saved = await save_latest_fit(db, user_id="user-a", result=_result(["met", "not_met"]))

    assert saved is True
    assert db.committed is True
    sql, params = db.calls[0]
    assert "ON CONFLICT (user_id, job_id) DO UPDATE" in sql
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

    assert saved is False
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
    }])

    fits = await list_latest_fits(db, user_id="user-a")

    assert db.calls[0][1] == {"user_id": "user-a"}
    assert "WHERE fit.user_id = :user_id" in db.calls[0][0]
    assert fits == [{
        "jobId": "job-1",
        "cvId": "cv-1",
        "cvName": "Backend CV.pdf",
        "met": 2,
        "total": 8,
        "analyzedAt": analyzed_at.isoformat(),
    }]
