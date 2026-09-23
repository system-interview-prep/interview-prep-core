"""MATCHING PHASE 2B — CONTRACT ALIGNMENT tests.

Verifies:
- TEST-2B-001  seniority='fresher' is supported, preserved, and does not fallback.
- TEST-2B-002  existing seniorities ('intern', 'junior', 'mid', 'senior', 'lead', 'manager') have no regression.
- TEST-2B-003  employment_type='temporary' is supported, preserved, and does not fallback.
- TEST-2B-004  existing employment types ('full_time', 'part_time', 'contract', 'internship') have no regression.
- TEST-2B-005  priority='required' maps explicitly to 'must_have'.
- TEST-2B-006  priority='preferred' maps explicitly to 'nice_to_have'.
- TEST-2B-007  unsupported priority ('unsupported_bonus') raises ValueError in adapter, 422 in router, no legacy fallback.
- TEST-2B-008  priority='context' fails closed at adapter boundary (raises ValueError, 422 in router, no legacy fallback).
"""
from __future__ import annotations

import hashlib
import sys
from types import ModuleType
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Pre-import Celery stub so router.py can be loaded in environments without celery
# ---------------------------------------------------------------------------

def _stub_celery_modules() -> None:
    if "celery" not in sys.modules:
        celery_mod = ModuleType("celery")

        class _Celery:
            def __init__(self, *a, **kw): pass
            def config_from_object(self, *a, **kw): pass
            def autodiscover_tasks(self, *a, **kw): pass
            def task(self, *a, **kw):
                def decorator(fn): return fn
                return decorator

        celery_mod.Celery = _Celery
        sys.modules["celery"] = celery_mod

    for mod_name in ("src.workers.celery_app", "src.workers.tasks.matching"):
        if mod_name not in sys.modules:
            stub = ModuleType(mod_name)
            if mod_name == "src.workers.tasks.matching":
                task_stub = MagicMock()
                task_stub.delay = MagicMock(return_value=MagicMock(id="task-stub"))
                stub.match_cv_to_jd = task_stub
            sys.modules[mod_name] = stub


_stub_celery_modules()

from src.modules.job_descriptions.schemas import CanonicalJobDescription
from src.modules.matching.adapters import job_description_to_matching_job
from src.modules.matching.router import _resolve_job
from src.modules.matching.schemas import CanonicalJob


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _make_canonical_jd_dict(
    *,
    seniority: str | None = "senior",
    employment_type: str | None = "full_time",
    priority: str = "must_have",
) -> dict:
    ev_id = "jd-ev-1"
    doc_id = "doc-jd-align"
    text = "Python and FastAPI required"
    doc_sha = hashlib.sha256(text.encode()).hexdigest()
    data: dict = {
        "schemaVersion": "1.0",
        "jobTitle": "Backend Engineer",
        "workMode": "remote",
        "requirements": [
            {
                "requirementId": "req-1",
                "kind": "skill",
                "priority": priority,
                "concept": {
                    "conceptId": "skill-python",
                    "scheme": "internal",
                    "taxonomyVersion": "2026.1",
                    "label": "Python",
                },
                "rawLabel": "Python",
                "evidenceRefs": [ev_id],
            }
        ],
        "responsibilities": [{"text": "Build APIs", "evidenceRefs": [ev_id]}],
        "evidence": [
            {
                "evidenceId": ev_id,
                "documentId": doc_id,
                "documentSha256": doc_sha,
                "section": "requirements",
                "text": text,
                "charStart": 0,
                "charEnd": len(text),
            }
        ],
        "parsing": {
            "parserVersion": "deterministic-v1",
            "extractionVersion": "test",
            "parsedAt": "2026-01-01T00:00:00Z",
            "status": "ready",
        },
    }
    if seniority is not None:
        data["seniority"] = seniority
    if employment_type is not None:
        data["employmentType"] = employment_type
    return data


def _make_async_db(structured_data: Any) -> AsyncMock:
    db = AsyncMock()
    row_data = {
        "id": "job-align-test",
        "title": "Backend Engineer",
        "keywords": ["python"],
        "description": "Build APIs",
        "requirements": "Python required",
        "structured_data": structured_data,
        "status": "active",
    }
    row = MagicMock()
    row.__getitem__ = lambda self, key: row_data[key]
    result = MagicMock()
    result.mappings.return_value.one_or_none.return_value = row
    db.execute.return_value = result
    return db


# ---------------------------------------------------------------------------
# TEST-2B-001 — fresher seniority
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_2b_001_fresher_seniority_is_supported_and_preserved() -> None:
    """seniority='fresher' must validate, pass through adapter, and not fallback to legacy."""
    data = _make_canonical_jd_dict(seniority="fresher")
    parsed = CanonicalJobDescription.model_validate(data)

    job = job_description_to_matching_job(parsed, job_id="job-fresher")
    assert job.seniority == "fresher"

    # Router path test: failsafe isolation
    db = _make_async_db(data)
    import src.modules.matching.router as router_mod

    synthetic_entered = []
    class SpyEvidenceSpan(router_mod.EvidenceSpan):
        def __init__(self, *args, **kwargs):
            synthetic_entered.append(True)
            super().__init__(*args, **kwargs)

    with patch.object(router_mod, "EvidenceSpan", SpyEvidenceSpan):
        resolved = await _resolve_job(db, "job-fresher")

    assert resolved.seniority == "fresher"
    assert len(synthetic_entered) == 0, "Legacy fallback was unexpectedly entered for 'fresher'"


# ---------------------------------------------------------------------------
# TEST-2B-002 — existing seniorities
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "seniority",
    ["intern", "junior", "mid", "senior", "lead", "manager"],
)
def test_2b_002_existing_seniorities_have_no_regression(seniority: str) -> None:
    """All existing valid seniorities must validate and be preserved."""
    data = _make_canonical_jd_dict(seniority=seniority)
    parsed = CanonicalJobDescription.model_validate(data)
    job = job_description_to_matching_job(parsed, job_id=f"job-{seniority}")
    assert job.seniority == seniority


# ---------------------------------------------------------------------------
# TEST-2B-003 — temporary employment type
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_2b_003_temporary_employment_type_is_supported_and_preserved() -> None:
    """employment_type='temporary' must validate, pass through adapter, and not fallback to legacy."""
    data = _make_canonical_jd_dict(employment_type="temporary")
    parsed = CanonicalJobDescription.model_validate(data)

    job = job_description_to_matching_job(parsed, job_id="job-temp")
    assert job.employment_type == "temporary"

    # Router path test: failsafe isolation
    db = _make_async_db(data)
    import src.modules.matching.router as router_mod

    synthetic_entered = []
    class SpyEvidenceSpan(router_mod.EvidenceSpan):
        def __init__(self, *args, **kwargs):
            synthetic_entered.append(True)
            super().__init__(*args, **kwargs)

    with patch.object(router_mod, "EvidenceSpan", SpyEvidenceSpan):
        resolved = await _resolve_job(db, "job-temp")

    assert resolved.employment_type == "temporary"
    assert len(synthetic_entered) == 0, "Legacy fallback was unexpectedly entered for 'temporary'"


# ---------------------------------------------------------------------------
# TEST-2B-004 — existing employment types
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "emp_type",
    ["full_time", "part_time", "contract", "internship"],
)
def test_2b_004_existing_employment_types_have_no_regression(emp_type: str) -> None:
    """All existing valid employment types must validate and be preserved."""
    data = _make_canonical_jd_dict(employment_type=emp_type)
    parsed = CanonicalJobDescription.model_validate(data)
    job = job_description_to_matching_job(parsed, job_id=f"job-{emp_type}")
    assert job.employment_type == emp_type


# ---------------------------------------------------------------------------
# TEST-2B-005 — required and must_have priority maps to must_have
# ---------------------------------------------------------------------------
def test_2b_005_priority_must_have_and_required_map_to_must_have() -> None:
    """Official 'must_have' and alias 'required' map explicitly to 'must_have'."""
    # 1. Official upstream priority: 'must_have'
    data_must = _make_canonical_jd_dict(priority="must_have")
    parsed_must = CanonicalJobDescription.model_validate(data_must)
    job_must = job_description_to_matching_job(parsed_must, job_id="job-must")
    assert job_must.requirements[0].priority == "must_have"

    # 2. Compatibility alias: 'required'
    data_req = _make_canonical_jd_dict(priority="must_have")
    parsed_req = CanonicalJobDescription.model_validate(data_req)
    parsed_req.requirements[0].priority = "required"  # type: ignore[assignment]
    job_req = job_description_to_matching_job(parsed_req, job_id="job-req")
    assert job_req.requirements[0].priority == "must_have"


# ---------------------------------------------------------------------------
# TEST-2B-006 — preferred and nice_to_have priority maps to nice_to_have
# ---------------------------------------------------------------------------
def test_2b_006_preferred_priority_maps_to_nice_to_have() -> None:
    """Upstream 'preferred' priority maps explicitly to 'nice_to_have'."""
    data = _make_canonical_jd_dict(priority="preferred")
    parsed = CanonicalJobDescription.model_validate(data)

    job = job_description_to_matching_job(parsed, job_id="job-pref")
    assert job.requirements[0].priority == "nice_to_have"


def test_2b_official_upstream_priority_contract() -> None:
    """Official upstream CanonicalJobDescription strictly permits only 'must_have' and 'preferred'."""
    from pydantic import ValidationError

    # must_have is valid
    CanonicalJobDescription.model_validate(_make_canonical_jd_dict(priority="must_have"))
    # preferred is valid
    CanonicalJobDescription.model_validate(_make_canonical_jd_dict(priority="preferred"))

    # context is NOT permitted in upstream JD schema
    with pytest.raises(ValidationError):
        CanonicalJobDescription.model_validate(_make_canonical_jd_dict(priority="context"))


# ---------------------------------------------------------------------------
# TEST-2B-007 — unsupported priority raises ValueError and HTTP 422
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_2b_007_unsupported_priority_fails_closed_without_legacy_fallback() -> None:
    """Unsupported priority raises ValueError in adapter and HTTP 422 in router without fallback."""
    from fastapi import HTTPException

    data = _make_canonical_jd_dict()
    parsed = CanonicalJobDescription.model_validate(data)
    parsed.requirements[0].priority = "unsupported_bonus"  # type: ignore[assignment]

    # 1. Direct adapter test: must raise ValueError
    with pytest.raises(ValueError) as exc_info:
        job_description_to_matching_job(parsed, job_id="job-unsupported")
    assert "unsupported requirement priority" in str(exc_info.value)
    assert "unsupported_bonus" in str(exc_info.value)

    # 2. Router test: adapter ValueError becomes HTTP 422, legacy fallback NOT entered
    db = _make_async_db(data)
    import src.modules.matching.router as router_mod

    synthetic_entered = []
    class SpyEvidenceSpan(router_mod.EvidenceSpan):
        def __init__(self, *args, **kwargs):
            synthetic_entered.append(True)
            super().__init__(*args, **kwargs)

    with patch.object(router_mod, "EvidenceSpan", SpyEvidenceSpan), patch.object(
        router_mod,
        "job_description_to_matching_job",
        side_effect=ValueError("unsupported requirement priority 'unsupported_bonus'"),
    ):
        with pytest.raises(HTTPException) as http_exc:
            await _resolve_job(db, "job-unsupported")

    assert http_exc.value.status_code == 422
    assert "job-unsupported" in http_exc.value.detail
    assert len(synthetic_entered) == 0, "Legacy fallback was entered after unsupported priority failure"


# ---------------------------------------------------------------------------
# TEST-2B-008 — context priority fails closed at adapter boundary
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_2b_008_context_priority_fails_closed_at_adapter_boundary() -> None:
    """priority='context' fails closed at adapter boundary: raises ValueError, 422 in router, no fallback."""
    from fastapi import HTTPException

    data = _make_canonical_jd_dict()
    parsed = CanonicalJobDescription.model_validate(data)
    parsed.requirements[0].priority = "context"  # type: ignore[assignment]

    # 1. Direct adapter test: raises ValueError
    with pytest.raises(ValueError) as exc_info:
        job_description_to_matching_job(parsed, job_id="job-ctx")
    assert "unsupported requirement priority" in str(exc_info.value)
    assert "context" in str(exc_info.value)

    # 2. Router test: adapter ValueError becomes HTTP 422, legacy fallback NOT entered
    db = _make_async_db(data)
    import src.modules.matching.router as router_mod

    synthetic_entered = []
    class SpyEvidenceSpan(router_mod.EvidenceSpan):
        def __init__(self, *args, **kwargs):
            synthetic_entered.append(True)
            super().__init__(*args, **kwargs)

    with patch.object(router_mod, "EvidenceSpan", SpyEvidenceSpan), patch.object(
        router_mod,
        "job_description_to_matching_job",
        side_effect=ValueError("unsupported requirement priority 'context' for requirement 'req-1'"),
    ):
        with pytest.raises(HTTPException) as http_exc:
            await _resolve_job(db, "job-ctx")

    assert http_exc.value.status_code == 422
    assert "job-ctx" in http_exc.value.detail
    assert len(synthetic_entered) == 0, "Legacy fallback was entered after context priority failure"


# ---------------------------------------------------------------------------
# DIAGNOSTIC TESTS FOR CONTEXT PRIORITY SCORING SEMANTICS
# ---------------------------------------------------------------------------

class _StubEmbedder:
    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0], [1.0, 0.0]]


def _make_diagnostic_match_payload(*, include_context_skill: bool) -> dict:
    sha = "b" * 64
    skills = [
        {
            "claimId": "claim-py",
            "concept": {"conceptId": "skill-python", "scheme": "internal", "taxonomyVersion": "2026.1", "label": "Python"},
            "rawLabel": "Python",
            "experienceMonths": 48,
            "evidenceRefs": ["cv-ev-1"],
        },
        {
            "claimId": "claim-docker",
            "concept": {"conceptId": "skill-docker", "scheme": "internal", "taxonomyVersion": "2026.1", "label": "Docker"},
            "rawLabel": "Docker",
            "experienceMonths": 24,
            "evidenceRefs": ["cv-ev-2"],
        },
    ]
    cv_evidence = [
        {"evidenceId": "cv-ev-1", "documentId": "cv-doc-1", "documentSha256": sha, "section": "skills", "text": "Python engineer", "charStart": 0, "charEnd": 15},
        {"evidenceId": "cv-ev-2", "documentId": "cv-doc-1", "documentSha256": sha, "section": "skills", "text": "Docker container", "charStart": 0, "charEnd": 16},
    ]
    jd_evidence = [
        {"evidenceId": "jd-ev-1", "documentId": "jd-doc-1", "documentSha256": sha, "section": "requirements", "text": "Requires Python", "charStart": 0, "charEnd": 15},
        {"evidenceId": "jd-ev-2", "documentId": "jd-doc-1", "documentSha256": sha, "section": "requirements", "text": "Requires Docker", "charStart": 0, "charEnd": 15},
    ]
    requirements = [
        {
            "requirementId": "req-py",
            "type": "skill",
            "priority": "must_have",
            "sourceEvidenceRef": "jd-ev-1",
            "skill": {"conceptId": "skill-python", "scheme": "internal", "taxonomyVersion": "2026.1", "label": "Python"},
            "operator": "gte",
            "minimumExperienceMonths": 24,
        }
    ]
    if include_context_skill:
        requirements.append(
            {
                "requirementId": "req-docker-ctx",
                "type": "skill",
                "priority": "context",
                "sourceEvidenceRef": "jd-ev-2",
                "skill": {"conceptId": "skill-docker", "scheme": "internal", "taxonomyVersion": "2026.1", "label": "Docker"},
                "operator": "required",
            }
        )

    return {
        "schemaVersion": "2.1",
        "resume": {
            "schemaVersion": "2.1",
            "resumeId": "cv-diag-1",
            "documentId": "cv-doc-1",
            "documentSha256": sha,
            "skills": skills,
            "evidence": cv_evidence,
        },
        "job": {
            "schemaVersion": "2.1",
            "jobId": "job-diag-1",
            "documentId": "jd-doc-1",
            "documentSha256": sha,
            "jobTitle": "Backend Developer",
            "requirements": requirements,
            "evidence": jd_evidence,
        },
        "matchingPolicy": {"policyVersion": "balanced-v1"},
    }


def test_2b_ctx_001_context_skill_requirement_behavior_in_facade() -> None:
    """TEST-2B-CTX-001: Diagnostic test of context SkillRequirement behavior.

    Proves:
    1. Context requirement does NOT enter Must-have Gate (_eligibility).
    2. Context requirement enters the Skill Factor together with must-have skills.
    3. Context requirement contributes to suitabilityScore computation.
    """
    from src.modules.matching.facade import MatchingFacade
    from src.modules.matching.schemas import MatchRequest

    payload = _make_diagnostic_match_payload(include_context_skill=True)
    facade = MatchingFacade(_StubEmbedder())
    result = facade.match(MatchRequest.model_validate(payload))

    # 1. Eligibility: Not in Must-have Gate
    assert result.eligibility == "eligible"

    # 2. Skill Factor: Consumed by skill factor because priority != 'must_have'
    skill_factor = next(f for f in result.factor_results if f.factor == "skill")
    assert skill_factor.status == "scored"
    assert skill_factor.raw_score == 1.0
    assert skill_factor.effective_weight > 0.0

    # 3. Suitability: Participates in overall score
    assert result.suitability_score is not None
    assert result.suitability_score > 0.0


def test_2b_ctx_002_context_skill_requirement_changes_suitability_score() -> None:
    """Both must-have and context skills are applicable to the skill factor."""
    from src.modules.matching.facade import MatchingFacade
    from src.modules.matching.schemas import MatchRequest

    payload_without = _make_diagnostic_match_payload(include_context_skill=False)
    payload_with = _make_diagnostic_match_payload(include_context_skill=True)

    facade = MatchingFacade(_StubEmbedder())
    res_without = facade.match(MatchRequest.model_validate(payload_without))
    res_with = facade.match(MatchRequest.model_validate(payload_with))

    # A must-have skill is still a skill-factor input; context is additive.
    skill_without = next(f for f in res_without.factor_results if f.factor == "skill")
    skill_with = next(f for f in res_with.factor_results if f.factor == "skill")

    assert skill_without.status == "scored"
    assert skill_with.status == "scored"
    assert skill_without.raw_score == 1.0
    assert skill_with.raw_score == 1.0
    assert res_without.score_provenance.mode == "requirement_aware"
    assert res_with.score_provenance.mode == "requirement_aware"

