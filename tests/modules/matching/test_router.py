from types import SimpleNamespace

import pytest

from src.modules.matching import router as matching_router
from src.modules.matching.schemas import MatchResult
from tests.modules.matching.test_matching_pipeline import _payload


def test_match_async_enqueues_unified_payload_and_returns_202(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = {}
    monkeypatch.setattr(
        matching_router.match_cv_to_jd,
        "delay",
        lambda payload: captured.update(payload) or SimpleNamespace(id="task-123"),
    )
    response = client.post("/api/v1/matching/match", json={**_payload(), "asyncProcessing": True})
    assert response.status_code == 202
    assert response.json() == {"taskId": "task-123", "status": "PENDING"}
    assert captured["resume"]["resumeId"] == "cv-1"
    assert captured["job"]["jobId"] == "job-1"


def test_match_rejects_legacy_raw_text_contract(client) -> None:
    response = client.post(
        "/api/v1/matching/match",
        json={"resumeText": "Python", "jobDescription": "Python"},
    )
    assert response.status_code == 422


async def test_match_sync_returns_the_same_unified_contract(client, monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_run_match(_):
        return MatchResult(
            resumeId="cv-1",
            jobId="job-1",
            policyVersion="balanced-v1",
            eligibility="eligible",
            suitabilityScore=0.8,
            fitBand="strong_fit",
            decision="assessed",
            requirementResults=[],
            factorResults=[],
        )

    monkeypatch.setattr(matching_router, "run_match", fake_run_match)
    response = client.post("/api/v1/matching/match", json={**_payload(), "asyncProcessing": False})
    assert response.status_code == 200
    assert response.json()["pipelineVersion"] == "one-to-one-evidence-fusion-v1"
    assert response.json()["eligibility"] == "eligible"
