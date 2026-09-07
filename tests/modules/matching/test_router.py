from types import SimpleNamespace

import pytest

from src.modules.matching import router as matching_router

VALID_PAYLOAD = {
    "resumeText": "Built Python and FastAPI services backed by PostgreSQL.",
    "jobDescription": "Requires Python, FastAPI, REST API and PostgreSQL experience.",
    "cvId": "cv-1",
    "jobDescriptionId": "job-1",
    "position": "Backend Engineer",
    "algorithms": ["requirements", "cosine", "bm25"],
}


def test_match_async_enqueues_job_and_returns_202(client, monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}
    monkeypatch.setattr(
        matching_router.match_cv_to_jd,
        "delay",
        lambda payload: captured.update(payload) or SimpleNamespace(id="task-123"),
    )
    response = client.post("/api/v1/matching/match", json={**VALID_PAYLOAD, "asyncProcessing": True})
    assert response.status_code == 202
    assert response.json() == {"taskId": "task-123", "status": "PENDING"}
    assert captured["algorithms"] == ["requirements", "cosine", "bm25"]


def test_match_defaults_to_async_processing(client, monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}
    monkeypatch.setattr(
        matching_router.match_cv_to_jd,
        "delay",
        lambda payload: captured.update(payload) or SimpleNamespace(id="task-default"),
    )
    assert (
        client.post(
            "/api/v1/matching/match", json={"resumeText": "Python", "jobDescription": "Python"}
        ).status_code
        == 202
    )
    assert captured["algorithms"] == ["embedding_cosine"]


@pytest.mark.parametrize(
    "payload",
    [
        {"jobDescription": "Python"},
        {"resumeText": "Python"},
        {"resumeText": "", "jobDescription": "Python"},
        {"resumeText": "Python", "jobDescription": "", "unknown": True},
    ],
)
def test_match_rejects_invalid_payloads(client, payload: dict) -> None:
    assert client.post("/api/v1/matching/match", json=payload).status_code == 422


def test_match_rejects_old_snake_case_public_fields(client) -> None:
    response = client.post(
        "/api/v1/matching/match",
        json={"resume_text": "Python", "job_description": "Python"},
    )
    assert response.status_code == 422
