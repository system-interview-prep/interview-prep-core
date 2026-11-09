from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import src.modules.matching.api.router as matching_router
from src.core.security import current_user
from src.modules.matching.application.input_resolver import resolve_resume, resume_owner_scope
from src.modules.matching.domain.schemas import MatchResult
from tests.modules.matching.test_matching_pipeline import _payload

_CANDIDATE = {"sub": "user-a", "email": "a@example.test", "roles": []}


@pytest.fixture
def client(client):
    """Matching is authenticated: run these contract tests as a signed-in candidate."""
    client.app.dependency_overrides[current_user] = lambda: _CANDIDATE
    yield client
    client.app.dependency_overrides.clear()


@pytest.fixture
def anonymous_client():
    from fastapi.testclient import TestClient

    from src.main import create_app

    anonymous = TestClient(create_app())
    yield anonymous
    anonymous.close()


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/matching/match",
        "/api/v1/matching/match-ids",
        "/ai/score-cv-jp",
        "/api/v1/matching/clarifications",
        "/api/v1/matching/clarifications-by-ids",
        "/api/v1/matching/clarifications/prepare-by-ids",
        "/api/v1/matching/clarifications/questions-by-ids",
        "/api/v1/matching/clarifications/rescore",
        "/api/v1/matching/clarifications/rescore-by-ids",
    ],
)
def test_matching_endpoints_reject_anonymous_callers(anonymous_client, path: str) -> None:
    response = anonymous_client.post(path, json={})
    assert response.status_code == 401


def test_fit_history_rejects_anonymous_callers(anonymous_client) -> None:
    for path in (
        "/api/v1/matching/fit-history",
        "/api/v1/matching/fit-history/attempts",
        "/api/v1/matching/fit-history/attempts/attempt-1",
    ):
        response = anonymous_client.get(path)
        assert response.status_code == 401


def test_candidate_fit_attempt_history_is_scoped_and_returned(client, monkeypatch) -> None:
    calls = {}

    async def fake_list(_db, *, user_id: str, limit: int):
        calls.update(user_id=user_id, limit=limit)
        return [{"id": "attempt-1", "jobId": "job-1", "met": 2, "total": 8}]

    monkeypatch.setattr(matching_router, "list_candidate_fit_attempts", fake_list)

    response = client.get("/api/v1/matching/fit-history/attempts?limit=25")

    assert response.status_code == 200
    assert calls == {"user_id": "user-a", "limit": 25}
    assert response.json() == {"items": [{"id": "attempt-1", "jobId": "job-1", "met": 2, "total": 8}]}


class _CapturingDb:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def execute(self, statement, params):
        self.calls.append((str(statement), params))
        return SimpleNamespace(mappings=lambda: SimpleNamespace(one_or_none=lambda: None))


async def test_resolve_resume_scopes_lookup_to_the_owner_and_hides_other_users_cvs() -> None:
    db = _CapturingDb()
    with pytest.raises(HTTPException) as exc:
        await resolve_resume(db, "cv-of-user-b", owner_id="user-a")
    assert exc.value.status_code == 404
    sql, params = db.calls[0]
    assert "user_id = :owner_id" in sql
    assert params == {"id": "cv-of-user-b", "owner_id": "user-a"}


def test_only_admins_may_match_any_cv() -> None:
    assert resume_owner_scope(_CANDIDATE) == "user-a"
    assert resume_owner_scope({"sub": "admin-1", "roles": ["ADMIN"]}) is None


async def test_match_ids_passes_the_signed_in_user_as_cv_owner(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = {}

    async def fake_resolve_resume(_db, cv_id, *, owner_id):
        seen["owner_id"] = owner_id
        raise HTTPException(status_code=404, detail="not found")

    monkeypatch.setattr(matching_router, "_resolve_resume", fake_resolve_resume)
    response = client.post("/api/v1/matching/match-ids", json={"candidateId": "cv-x", "jobId": "job-x"})
    assert response.status_code == 404
    assert seen["owner_id"] == "user-a"


async def test_sample_ids_are_resolved_normally_and_never_short_circuited(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A "sample-" id gets no special treatment: every score comes from the matcher.

    The endpoint used to return a hand-written MatchResult for ids starting with
    "sample-", so a caller could receive a 200 with scores no part of the
    pipeline had produced.  Ids are now just ids - an unknown one fails
    resolution like any other.
    """
    seen = {}

    async def fake_resolve_resume(_db, cv_id, *, owner_id):
        seen["cv_id"] = cv_id
        raise HTTPException(status_code=404, detail="not found")

    monkeypatch.setattr(matching_router, "_resolve_resume", fake_resolve_resume)
    response = client.post(
        "/api/v1/matching/match-ids",
        json={"candidateId": "sample-cv-1", "jobId": "sample-job-1"},
    )

    assert response.status_code == 404
    # Resolution was actually attempted rather than bypassed.
    assert seen["cv_id"] == "sample-cv-1"
    assert not hasattr(matching_router, "_sample_match_result")


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


@pytest.mark.parametrize(
    ("user", "expected_mode"),
    [
        (_CANDIDATE, "in_memory"),
        ({"sub": "admin-1", "email": "admin@example.test", "roles": ["ADMIN"]}, "paradedb"),
    ],
)
def test_match_does_not_score_a_stored_cv_by_a_client_supplied_id(
    client, monkeypatch: pytest.MonkeyPatch, user: dict, expected_mode: str
) -> None:
    """/match takes a client-supplied resume: a candidate naming another user's
    CV id as documentId must not get a ParadeDB score of that stored CV."""
    seen: list[str] = []

    async def fake_run_match(payload):
        seen.append(payload.matching_policy.bm25_provider_mode)
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
    client.app.dependency_overrides[current_user] = lambda: user
    # The documentId in the body is whatever the client claims it is.
    body = _payload()
    body["matchingPolicy"] = {**body.get("matchingPolicy", {}), "bm25ProviderMode": "paradedb"}

    response = client.post("/api/v1/matching/match", json={**body, "asyncProcessing": False})

    assert response.status_code == 200
    assert seen == [expected_mode]
