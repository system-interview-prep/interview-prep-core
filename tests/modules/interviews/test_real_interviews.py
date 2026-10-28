"""Real interview reports (research 20/10): readiness snapshot and API checks."""

from datetime import date

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from src.modules.interviews.api.router import RealInterviewPayload
from src.modules.interviews.application.real_interviews import readiness_snapshot


def _progress() -> dict:
    return {
        "readiness": [
            {
                "jobId": "job-a",
                "sessions": 2,
                "rounds": [{"area": "data", "status": "almost", "estimate": 6.1}],
            },
            {
                "jobId": "job-b",
                "sessions": 5,
                "rounds": [{"area": "infra", "status": "ready", "estimate": 7.4}],
            },
        ]
    }


def test_snapshot_keeps_the_rounds_of_the_reported_position() -> None:
    snap = readiness_snapshot(_progress(), "job-a")
    assert snap == {
        "jobId": "job-a",
        "sessions": 2,
        "rounds": {"data": {"status": "almost", "estimate": 6.1}},
    }


def test_snapshot_falls_back_to_the_most_practised_position() -> None:
    assert readiness_snapshot(_progress(), None)["jobId"] == "job-b"
    assert readiness_snapshot({"readiness": []}, "job-a") == {}


def test_payload_accepts_only_known_stages_and_results() -> None:
    ok = RealInterviewPayload.model_validate(
        {"date": "2026-10-18", "stage": "technical", "result": "waiting", "askedAreas": ["data"]}
    )
    assert ok.interview_date == date(2026, 10, 18)
    with pytest.raises(ValidationError):
        RealInterviewPayload.model_validate({"date": "2026-10-18", "stage": "lunch", "result": "passed"})


async def test_a_future_date_is_refused() -> None:
    from src.modules.interviews.api.router import _store_real_interview

    payload = RealInterviewPayload.model_validate(
        {"date": "2099-01-01", "stage": "final", "result": "passed"}
    )
    with pytest.raises(HTTPException) as exc:
        await _store_real_interview(None, {"sub": "u1"}, payload, None)  # type: ignore[arg-type]
    assert exc.value.status_code == 422
