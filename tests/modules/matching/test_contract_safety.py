"""MATCHING PHASE 2A — CONTRACT SAFETY tests.

Verifies that _resolve_job() fails closed when structured_data is present
but canonical validation or adapter conversion fails.

The router imports Celery at module level, so we mock celery before importing
the router — same pattern as all router-level tests that need direct function access.

TEST-2A-001  Valid structured_data → canonical path, legacy synthetic path NOT entered.
TEST-2A-002  structured_data exists but CanonicalJobDescription validation fails → 422, no fallback.
TEST-2A-003  structured_data valid but adapter raises ValueError → 422, no fallback.
TEST-2A-004  structured_data missing/null → legacy fallback allowed (backward compat).
TEST-2A-005  structured_data has invalid seniority enum → 422, no fallback.
TEST-2A-006  structured_data has invalid employment_type → 422, no fallback.
TEST-2A-007  Regression: valid canonical JD returns correct CanonicalJob (not synthetic).
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
    """Inject minimal stubs for celery so router.py imports cleanly without the broker."""
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

# Now we can safely import the router function
from src.modules.matching.router import _resolve_job  # noqa: E402
from src.modules.matching.schemas import CanonicalJob  # noqa: E402

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

SHA256 = "a" * 64


def _make_db_row(structured_data: Any, *, title: str = "Test Job") -> MagicMock:
    """Return a fake SQLAlchemy row mapping for a job_descriptions record."""
    data = {
        "id": "job-test",
        "title": title,
        "keywords": ["python"],
        "description": "Build APIs",
        "requirements": "Python required",
        "structured_data": structured_data,
        "status": "active",
    }
    row = MagicMock()
    row.__getitem__ = lambda self, key: data[key]
    return row


def _make_async_db(structured_data: Any, *, title: str = "Test Job") -> AsyncMock:
    """Return an AsyncMock DB session whose execute chain returns a single job row."""
    db = AsyncMock()
    result = MagicMock()
    result.mappings.return_value.one_or_none.return_value = _make_db_row(
        structured_data, title=title
    )
    db.execute.return_value = result
    return db


def _canonical_structured_data(
    *,
    seniority: str | None = "senior",
    employment_type: str | None = "full_time",
) -> dict:
    """Return a minimal valid CanonicalJobDescription (schema v1.0) dict."""
    ev_id = "jd-ev-1"
    doc_id = "doc-jd-test"
    text = "Python required"
    doc_sha = hashlib.sha256(text.encode()).hexdigest()
    data: dict = {
        "schemaVersion": "1.0",
        "jobTitle": "Python Engineer",
        "workMode": "remote",
        "requirements": [
            {
                "requirementId": "req-python",
                "kind": "skill",
                "priority": "must_have",
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


# ---------------------------------------------------------------------------
# TEST-2A-001 — Valid structured_data → canonical path; legacy synthetic NOT entered
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_2a_001_valid_structured_data_uses_canonical_path_not_legacy() -> None:
    """Valid canonical JD → CanonicalJob from canonical adapter; legacy EvidenceSpan NOT instantiated."""
    db = _make_async_db(_canonical_structured_data())

    synthetic_entered = []

    original_evidence_span = None
    import src.modules.matching.router as router_mod
    original_evidence_span_cls = router_mod.EvidenceSpan

    class SpyEvidenceSpan(original_evidence_span_cls):
        def __init__(self, *args, **kwargs):
            synthetic_entered.append(True)
            super().__init__(*args, **kwargs)

    with patch.object(router_mod, "EvidenceSpan", SpyEvidenceSpan):
        result = await _resolve_job(db, "job-test")

    # Legacy path was NOT entered
    assert len(synthetic_entered) == 0, "Legacy EvidenceSpan constructor was called — fallback entered"
    # Result is a proper CanonicalJob from canonical adapter
    assert isinstance(result, CanonicalJob)
    assert result.job_id == "job-test"
    # Requirement comes from canonical data (has a proper concept), not synthetic "Core competencies"
    assert result.requirements[0].requirement_id == "req-python"


# ---------------------------------------------------------------------------
# TEST-2A-002 — structured_data exists, validation fails → 422, no legacy fallback
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_2a_002_invalid_structured_data_raises_422_not_fallback() -> None:
    """CanonicalJobDescription.model_validate fails → HTTP 422, legacy fallback NOT entered."""
    from fastapi import HTTPException

    # Missing required 'parsing' field and malformed structure
    broken_data = {"schemaVersion": "1.0", "jobTitle": "Broken Job"}
    db = _make_async_db(broken_data)

    synthetic_entered = []
    import src.modules.matching.router as router_mod

    class SpyEvidenceSpan(router_mod.EvidenceSpan):
        def __init__(self, *args, **kwargs):
            synthetic_entered.append(True)
            super().__init__(*args, **kwargs)

    with patch.object(router_mod, "EvidenceSpan", SpyEvidenceSpan):
        with pytest.raises(HTTPException) as exc_info:
            await _resolve_job(db, "job-bad")

    assert exc_info.value.status_code == 422
    detail = exc_info.value.detail
    assert "job-bad" in detail
    # Detail must mention validation/contract — not a generic 500
    assert any(word in detail.lower() for word in ("contract", "validation", "parsing"))
    # Legacy path was NOT entered
    assert len(synthetic_entered) == 0, "Legacy fallback was entered after canonical validation failure"


# ---------------------------------------------------------------------------
# TEST-2A-003 — structured_data valid, adapter raises ValueError → 422, no fallback
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_2a_003_adapter_value_error_raises_422_not_fallback() -> None:
    """Adapter raises ValueError → HTTP 422, legacy fallback NOT entered."""
    from fastapi import HTTPException

    db = _make_async_db(_canonical_structured_data())

    synthetic_entered = []
    import src.modules.matching.router as router_mod

    class SpyEvidenceSpan(router_mod.EvidenceSpan):
        def __init__(self, *args, **kwargs):
            synthetic_entered.append(True)
            super().__init__(*args, **kwargs)

    with patch.object(router_mod, "EvidenceSpan", SpyEvidenceSpan), patch.object(
        router_mod,
        "job_description_to_matching_job",
        side_effect=ValueError("requirement req-python has no evidence"),
    ) as mock_adapter:
        with pytest.raises(HTTPException) as exc_info:
            await _resolve_job(db, "job-adapter-fail")

    assert exc_info.value.status_code == 422
    # Adapter was called — canonical path was entered
    mock_adapter.assert_called_once()
    # Legacy path was NOT entered
    assert len(synthetic_entered) == 0, "Legacy fallback was entered after adapter ValueError"


# ---------------------------------------------------------------------------
# TEST-2A-004 — structured_data missing/null → legacy fallback runs normally
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_2a_004_missing_structured_data_uses_legacy_fallback() -> None:
    """structured_data=None → legacy synthetic path produces a usable CanonicalJob."""
    db = _make_async_db(None)

    result = await _resolve_job(db, "job-legacy")

    assert isinstance(result, CanonicalJob)
    assert result.job_id == "job-legacy"
    # Legacy path always produces at least one synthetic requirement
    assert len(result.requirements) >= 1


# ---------------------------------------------------------------------------
# TEST-2A-005 — invalid seniority enum → 422, no fallback
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_2a_005_invalid_seniority_in_structured_data_raises_not_falls_back() -> None:
    """structured_data with invalid seniority enum → HTTP 4xx/5xx, no fallback."""
    from fastapi import HTTPException

    data = _canonical_structured_data(seniority="invalid_seniority")
    db = _make_async_db(data)

    synthetic_entered = []
    import src.modules.matching.router as router_mod

    class SpyEvidenceSpan(router_mod.EvidenceSpan):
        def __init__(self, *args, **kwargs):
            synthetic_entered.append(True)
            super().__init__(*args, **kwargs)

    with patch.object(router_mod, "EvidenceSpan", SpyEvidenceSpan):
        with pytest.raises(HTTPException) as exc_info:
            await _resolve_job(db, "job-invalid-seniority")

    # Error is surfaced (any 4xx/5xx), not silently swallowed
    assert exc_info.value.status_code in (422, 500)
    # Legacy path was NOT entered
    assert len(synthetic_entered) == 0, "Legacy fallback was entered for invalid seniority"


# ---------------------------------------------------------------------------
# TEST-2A-006 — invalid employment_type enum → 422, no fallback
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_2a_006_invalid_employment_type_in_structured_data_raises_not_falls_back() -> None:
    """structured_data with invalid employment_type → HTTP 4xx/5xx, no fallback."""
    from fastapi import HTTPException

    data = _canonical_structured_data(employment_type="invalid_type")
    db = _make_async_db(data)

    synthetic_entered = []
    import src.modules.matching.router as router_mod

    class SpyEvidenceSpan(router_mod.EvidenceSpan):
        def __init__(self, *args, **kwargs):
            synthetic_entered.append(True)
            super().__init__(*args, **kwargs)

    with patch.object(router_mod, "EvidenceSpan", SpyEvidenceSpan):
        with pytest.raises(HTTPException) as exc_info:
            await _resolve_job(db, "job-invalid-emp-type")

    assert exc_info.value.status_code in (422, 500)
    assert len(synthetic_entered) == 0, "Legacy fallback was entered for invalid employment type"


# ---------------------------------------------------------------------------
# TEST-2A-007 — Regression: valid canonical JD returns canonical CanonicalJob (not synthetic)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_2a_007_valid_canonical_jd_returns_canonical_requirement_not_synthetic() -> None:
    """Regression: valid canonical JD must produce CanonicalJob with canonical requirements, not synthetic."""
    db = _make_async_db(_canonical_structured_data())

    result = await _resolve_job(db, "job-regression")

    assert isinstance(result, CanonicalJob)
    assert result.job_id == "job-regression"
    # Requirement ID matches canonical data ("req-python"), NOT synthetic ("req-job-regression-1")
    req_ids = [r.requirement_id for r in result.requirements]
    assert "req-python" in req_ids, (
        f"Expected canonical req 'req-python' but got: {req_ids}. "
        f"Synthetic path may have been used."
    )
    # Synthetic ID pattern must NOT appear
    assert not any(rid.startswith("req-job-") for rid in req_ids), (
        f"Synthetic requirement IDs found: {req_ids}"
    )
