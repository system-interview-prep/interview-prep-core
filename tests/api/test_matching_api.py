from types import SimpleNamespace

import pytest

from src.modules.matching import router as matching_router

VALID_PAYLOAD = {
    "resume_text": "Built Python and FastAPI services backed by PostgreSQL.",
    "job_description": "Requires Python, FastAPI, REST API and PostgreSQL experience.",
    "cv_id": "cv-1",
    "job_id": "job-1",
    "position": "Backend Engineer",
    "algorithms": ["requirements", "cosine", "bm25"],
}


def test_match_async_enqueues_job_and_returns_202(client, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def fake_delay(payload: dict) -> SimpleNamespace:
        captured.update(payload)
        return SimpleNamespace(id="task-123")

    monkeypatch.setattr(matching_router.match_cv_to_jd, "delay", fake_delay)

    response = client.post(
        "/api/v1/matching/match",
        json={**VALID_PAYLOAD, "async_processing": True},
    )

    assert response.status_code == 202
    assert response.json() == {"task_id": "task-123", "status": "PENDING"}
    assert captured["resume_text"] == VALID_PAYLOAD["resume_text"]
    assert captured["job_description"] == VALID_PAYLOAD["job_description"]
    assert captured["algorithms"] == ["requirements", "cosine", "bm25"]


def test_match_defaults_to_async_processing(client, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def fake_delay(payload: dict) -> SimpleNamespace:
        captured.update(payload)
        return SimpleNamespace(id="task-default")

    monkeypatch.setattr(matching_router.match_cv_to_jd, "delay", fake_delay)

    response = client.post(
        "/api/v1/matching/match",
        json={
            "resume_text": VALID_PAYLOAD["resume_text"],
            "job_description": VALID_PAYLOAD["job_description"],
        },
    )

    assert response.status_code == 202
    assert captured["async_processing"] is True
    assert captured["algorithms"] == ["embedding_cosine"]


def test_match_sync_returns_versioned_result(client, monkeypatch: pytest.MonkeyPatch) -> None:
    expected = {
        "metadata": {"algorithms_used": ["requirements", "cosine", "bm25"]},
        "combined_results": [{"combined_score": 0.72}],
    }

    async def fake_run_match(payload):
        assert payload.async_processing is False
        assert payload.cv_id == "cv-1"
        return expected

    monkeypatch.setattr(matching_router, "run_match", fake_run_match)

    response = client.post(
        "/api/v1/matching/match",
        json={**VALID_PAYLOAD, "async_processing": False},
    )

    assert response.status_code == 200
    assert response.json() == {
        "pipeline_version": "external-embedding-cosine-v1",
        "result": expected,
    }


@pytest.mark.parametrize(
    "payload,missing_field",
    [
        ({"job_description": "Python required"}, "resume_text"),
        ({"resume_text": "Python experience"}, "job_description"),
        ({"resume_text": "", "job_description": "Python required"}, "resume_text"),
        ({"resume_text": "Python experience", "job_description": ""}, "job_description"),
    ],
)
def test_match_rejects_missing_or_empty_text(client, payload: dict, missing_field: str) -> None:
    response = client.post("/api/v1/matching/match", json=payload)

    assert response.status_code == 422
    assert any(error["loc"][-1] == missing_field for error in response.json()["detail"])


def test_match_rejects_unknown_fields(client) -> None:
    response = client.post(
        "/api/v1/matching/match",
        json={**VALID_PAYLOAD, "unknown_option": True},
    )

    assert response.status_code == 422
    assert response.json()["detail"][0]["type"] == "extra_forbidden"


def test_match_rejects_unsupported_method(client) -> None:
    response = client.get("/api/v1/matching/match")

    assert response.status_code == 405
