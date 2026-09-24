"""
PR4 — Backfill field tests (v2 — dual source strategy).

Mandatory invariants:
  A. Default mode only updates NULL columns.
  B. Existing non-NULL value is preserved (SKIP_ALREADY_SET).
  C. --force can overwrite an existing value (OVERWRITE action).
  D. No evidence in structured_data or raw_text -> no update (SKIP_NO_EVIDENCE).
  E. structured_data is never mutated.
  F. Lifecycle columns are never touched.
  G. One job failure rolls back that job only.
  H. Other jobs in the batch can continue after a failure.
  I. --dry-run performs zero DB writes.
  J. NSTAGE expected proposal via EXISTING_CANONICAL path.
  K. Legacy structured_data (missing company/experience) + raw_text triggers
     REEXTRACTED_FROM_RAW path and extracts correct values.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from scripts.backfill_job_listing_fields import (
    BACKFILL_FIELDS,
    EXISTING_CANONICAL,
    REEXTRACTED_FROM_RAW,
    SKIP_NO_SOURCE,
    FieldProposal,
    JobProposal,
    RunResult,
    _apply_proposal,
    _build_proposal,
    _decode_structured_data,
    _evidence_text,
    _run,
    _run_parser,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_row(
    job_id: str = "jd-1",
    title: str = "Test Job",
    structured_data: dict | None = None,
    raw_text: str | None = None,
    **overrides: Any,
) -> dict:
    """Build a minimal DB row dict with all backfillable columns NULL by default."""
    base: dict[str, Any] = {
        "id": job_id,
        "title": title,
        "raw_text": raw_text,
        "company_name": None,
        "location": None,
        "work_mode": None,
        "employment_type": None,
        "seniority": None,
        "experience_min_years": None,
        "experience_max_years": None,
        "salary_min": None,
        "salary_max": None,
        "salary_currency": None,
        "salary_period": None,
        "salary_negotiable": None,
        "structured_data": structured_data,
    }
    base.update(overrides)
    return base


def _nstage_structured_data() -> dict:
    """Realistic PR2 structured_data for NSTAGE Unity Developer JD."""
    return {
        "schemaVersion": "1.0",
        "jobTitle": "Unity Developer",
        "companyName": "NSTAGE",
        "location": "Hà Nội",
        "workMode": None,
        "employmentType": None,
        "seniority": None,
        "experienceMinYears": 2,
        "experienceMaxYears": None,
        "experienceRaw": "Có ít nhất 2 năm kinh nghiệm lập trình Game...",
        "salaryMin": None,
        "salaryMax": None,
        "salaryCurrency": None,
        "salaryPeriod": None,
        "salaryNegotiable": None,
        "salaryRaw": None,
        "evidence": [
            {
                "evidenceId": "ev-jd-company-abc123",
                "text": "NSTAGE : Fun Lives On",
                "charStart": 0,
                "charEnd": 21,
            },
            {
                "evidenceId": "ev-jd-location-def456",
                "text": "Hà Nội: Tầng 12A, Tòa Hapulico Center Building",
                "charStart": 100,
                "charEnd": 148,
            },
            {
                "evidenceId": "ev-jd-experience-ghi789",
                "text": "Có ít nhất 2 năm kinh nghiệm lập trình Game",
                "charStart": 200,
                "charEnd": 244,
            },
        ],
    }


# Legacy structured_data — created before PR2 (missing company/experience/salary)
def _legacy_structured_data() -> dict:
    return {
        "schemaVersion": "1.0",
        "jobTitle": None,
        "companyName": None,   # not extracted in pre-PR2 parser
        "location": None,
        "workMode": None,
        "employmentType": None,
        "seniority": None,
        "experienceMinYears": None,  # not extracted
        "experienceMaxYears": None,
        "experienceRaw": None,
        "salaryMin": None,
        "salaryMax": None,
        "salaryCurrency": None,
        "salaryPeriod": None,
        "salaryNegotiable": None,
        "salaryRaw": None,
        "evidence": [],
    }


NSTAGE_RAW_TEXT = """NSTAGE : Fun Lives On

Chúng tôi đang tuyển dụng Unity Developer.

Địa điểm:
Hà Nội: Tầng 12A, Tòa Hapulico Center Building

Yêu cầu ứng tuyển:
- Có ít nhất 2 năm kinh nghiệm lập trình Game với Unity
- Thành thạo C#
"""


# ---------------------------------------------------------------------------
# TEST A — Default mode only updates NULL columns
# ---------------------------------------------------------------------------

def test_A_default_mode_updates_only_null_columns() -> None:
    sd = {"companyName": "NSTAGE", "location": "Hà Nội", "experienceMinYears": 2}
    row = _make_row(structured_data=sd)

    proposal = _build_proposal(row, force=False)
    by_col = {fp.db_col: fp for fp in proposal.fields}

    assert by_col["company_name"].action == "UPDATE"
    assert by_col["location"].action == "UPDATE"
    assert by_col["experience_min_years"].action == "UPDATE"
    assert by_col["company_name"].proposal_source == EXISTING_CANONICAL


# ---------------------------------------------------------------------------
# TEST B — Existing non-NULL value is preserved
# ---------------------------------------------------------------------------

def test_B_existing_value_is_preserved_without_force() -> None:
    sd = {"companyName": "NSTAGE"}
    row = _make_row(structured_data=sd, company_name="NSTAGE Studio")

    proposal = _build_proposal(row, force=False)
    by_col = {fp.db_col: fp for fp in proposal.fields}

    fp = by_col["company_name"]
    assert fp.action == "SKIP_ALREADY_SET"
    assert fp.current_value == "NSTAGE Studio"
    assert fp.proposed_value == "NSTAGE"
    assert fp.proposal_source == EXISTING_CANONICAL


# ---------------------------------------------------------------------------
# TEST C — --force can overwrite
# ---------------------------------------------------------------------------

def test_C_force_overwrites_existing_value() -> None:
    sd = {"companyName": "NSTAGE"}
    row = _make_row(structured_data=sd, company_name="NSTAGE Studio")

    proposal = _build_proposal(row, force=True)
    by_col = {fp.db_col: fp for fp in proposal.fields}

    fp = by_col["company_name"]
    assert fp.action == "OVERWRITE"
    assert fp.proposed_value == "NSTAGE"
    assert fp.proposal_source == EXISTING_CANONICAL


# ---------------------------------------------------------------------------
# TEST D — No evidence anywhere → no update
# ---------------------------------------------------------------------------

def test_D_no_evidence_in_either_source() -> None:
    """Both structured_data empty and no raw_text → SKIP_NO_EVIDENCE / SKIP_NO_SOURCE."""
    row = _make_row(structured_data={}, raw_text=None)

    proposal = _build_proposal(row, force=False)
    for fp in proposal.fields:
        assert fp.action == "SKIP_NO_EVIDENCE", f"{fp.db_col}: expected SKIP_NO_EVIDENCE"
        assert fp.proposal_source == SKIP_NO_SOURCE


# ---------------------------------------------------------------------------
# TEST E — structured_data is never mutated
# ---------------------------------------------------------------------------

def test_E_structured_data_is_never_mutated() -> None:
    import copy
    sd = {"companyName": "NSTAGE", "location": "Hà Nội"}
    sd_original = copy.deepcopy(sd)
    row = _make_row(structured_data=sd, raw_text=NSTAGE_RAW_TEXT)

    _build_proposal(row, force=True)

    assert sd == sd_original, "structured_data dict was mutated!"


# ---------------------------------------------------------------------------
# TEST F — Lifecycle + protected columns never in backfill set
# ---------------------------------------------------------------------------

def test_F_lifecycle_columns_not_in_backfill_fields() -> None:
    forbidden = {
        "processing_status", "listing_status", "status",
        "title", "posted_at", "primary_taxonomy_concept_id",
        "primary_taxonomy_version", "source_type", "source_key",
        "external_job_id", "structured_data", "extracted_metadata",
    }
    for col in forbidden:
        assert col not in BACKFILL_FIELDS


def test_F_proposal_fields_never_contain_lifecycle_columns() -> None:
    sd = {"companyName": "NSTAGE"}
    row = _make_row(structured_data=sd)
    proposal = _build_proposal(row, force=False)
    protected = {"processing_status", "listing_status", "status", "title", "structured_data"}
    for fp in proposal.fields:
        assert fp.db_col not in protected


# ---------------------------------------------------------------------------
# TEST G — One job failure rolls back that job
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_G_single_job_failure_rolls_back_that_job() -> None:
    sd = {"companyName": "NSTAGE"}
    row = _make_row(structured_data=sd)
    proposal = _build_proposal(row, force=False)

    bad_session = AsyncMock()
    bad_session.execute = AsyncMock(side_effect=RuntimeError("DB write failed"))
    bad_session.commit = AsyncMock()

    with pytest.raises(RuntimeError, match="DB write failed"):
        await _apply_proposal(bad_session, proposal)

    bad_session.commit.assert_not_called()


# ---------------------------------------------------------------------------
# TEST H — Other jobs continue after failure
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_H_other_jobs_continue_after_one_failure() -> None:
    sd = {"companyName": "NSTAGE"}
    rows = [
        _make_row("jd-fail", structured_data=sd),
        _make_row("jd-ok", title="Good Job", structured_data={"location": "HCM"}),
    ]

    async def mock_apply(db: Any, proposal: JobProposal) -> int:
        if proposal.job_id == "jd-fail":
            raise RuntimeError("simulated failure")
        return 1

    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=MagicMock())
    cm.__aexit__ = AsyncMock(return_value=None)

    with patch("scripts.backfill_job_listing_fields._apply_proposal", new=mock_apply):
        with patch("scripts.backfill_job_listing_fields._fetch_jobs", new=AsyncMock(return_value=rows)):
            with patch("scripts.backfill_job_listing_fields.SessionFactory", new=MagicMock(return_value=cm)):
                result = await _run(job_id=None, limit=50, dry_run=False, force=False)

    assert result.failed == 1
    assert result.updated >= 1


# ---------------------------------------------------------------------------
# TEST I — --dry-run zero DB writes
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_I_dry_run_performs_zero_db_writes() -> None:
    sd = {"companyName": "NSTAGE", "location": "Hà Nội"}
    rows = [_make_row(structured_data=sd)]

    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=MagicMock())
    cm.__aexit__ = AsyncMock(return_value=None)

    with patch("scripts.backfill_job_listing_fields._apply_proposal") as mock_apply:
        with patch("scripts.backfill_job_listing_fields._fetch_jobs", new=AsyncMock(return_value=rows)):
            with patch("scripts.backfill_job_listing_fields.SessionFactory", new=MagicMock(return_value=cm)):
                result = await _run(job_id=None, limit=50, dry_run=True, force=False)

    mock_apply.assert_not_called()
    assert result.updated == 1
    assert result.failed == 0


# ---------------------------------------------------------------------------
# TEST J — NSTAGE via EXISTING_CANONICAL path
# ---------------------------------------------------------------------------

def test_J_nstage_existing_canonical_proposal() -> None:
    """
    When structured_data already has companyName/location/experienceMinYears,
    the proposal must come from EXISTING_CANONICAL and propose correct values.
    """
    sd = _nstage_structured_data()
    row = _make_row(
        job_id="01aaa05b-c5e6-464f-a691-8c3bf56bb96b",
        title="Unity Developer - Game Mobil",
        structured_data=sd,
    )

    proposal = _build_proposal(row, force=False)
    by_col = {fp.db_col: fp for fp in proposal.fields}

    # UPDATE from EXISTING_CANONICAL
    assert by_col["company_name"].action == "UPDATE"
    assert by_col["company_name"].proposed_value == "NSTAGE"
    assert by_col["company_name"].proposal_source == EXISTING_CANONICAL
    assert "NSTAGE" in by_col["company_name"].evidence_excerpt

    assert by_col["location"].action == "UPDATE"
    assert by_col["location"].proposed_value == "Hà Nội"
    assert by_col["location"].proposal_source == EXISTING_CANONICAL

    assert by_col["experience_min_years"].action == "UPDATE"
    assert by_col["experience_min_years"].proposed_value == 2
    assert by_col["experience_min_years"].proposal_source == EXISTING_CANONICAL

    # Fields without evidence in structured_data
    for col in ("experience_max_years", "seniority", "employment_type", "work_mode"):
        assert by_col[col].action == "SKIP_NO_EVIDENCE", f"{col} should be SKIP_NO_EVIDENCE"

    for col in ("salary_min", "salary_max", "salary_currency", "salary_period", "salary_negotiable"):
        assert by_col[col].action == "SKIP_NO_EVIDENCE", f"{col} should be SKIP_NO_EVIDENCE"


# ---------------------------------------------------------------------------
# TEST K — Legacy structured_data + raw_text → REEXTRACTED_FROM_RAW
# ---------------------------------------------------------------------------

def test_K_legacy_structured_data_triggers_reextract() -> None:
    """
    When structured_data exists but was created before PR2 (companyName/experienceMinYears
    are None), AND raw_text contains evidence, the backfill must run the PR2 parser
    and extract the correct values via REEXTRACTED_FROM_RAW.

    Expected:
      company_name          → NSTAGE         (REEXTRACTED_FROM_RAW)
      experience_min_years  → 2              (REEXTRACTED_FROM_RAW)
    """
    row = _make_row(
        job_id="jd-legacy-nstage",
        title="Unity Developer - Game Mobil",
        structured_data=_legacy_structured_data(),  # all fields null
        raw_text=NSTAGE_RAW_TEXT,
    )

    proposal = _build_proposal(row, force=False)
    by_col = {fp.db_col: fp for fp in proposal.fields}

    # company_name must be extracted from raw_text
    company_fp = by_col["company_name"]
    assert company_fp.action == "UPDATE", f"Expected UPDATE, got {company_fp.action}"
    assert company_fp.proposed_value == "NSTAGE", (
        f"Expected 'NSTAGE', got {company_fp.proposed_value!r}"
    )
    assert company_fp.proposal_source == REEXTRACTED_FROM_RAW

    # experience_min_years must be extracted from raw_text
    exp_fp = by_col["experience_min_years"]
    assert exp_fp.action == "UPDATE", f"Expected UPDATE, got {exp_fp.action}"
    assert exp_fp.proposed_value == 2, f"Expected 2, got {exp_fp.proposed_value!r}"
    assert exp_fp.proposal_source == REEXTRACTED_FROM_RAW

    # Fields not present in text → still SKIP_NO_EVIDENCE
    for col in ("work_mode", "employment_type", "seniority"):
        assert by_col[col].action == "SKIP_NO_EVIDENCE", f"{col} should be SKIP_NO_EVIDENCE"


def test_K_no_raw_text_and_legacy_sd_is_all_skip_no_source() -> None:
    """If raw_text is absent and structured_data has no values, every field is SKIP_NO_SOURCE."""
    row = _make_row(
        structured_data=_legacy_structured_data(),
        raw_text=None,
    )
    proposal = _build_proposal(row, force=False)
    for fp in proposal.fields:
        assert fp.action == "SKIP_NO_EVIDENCE"
        assert fp.proposal_source == SKIP_NO_SOURCE


# ---------------------------------------------------------------------------
# EXTRA: SKIP_UNCHANGED when value already matches
# ---------------------------------------------------------------------------

def test_skip_unchanged_when_value_identical() -> None:
    sd = {"companyName": "NSTAGE", "location": "Hà Nội"}
    row = _make_row(structured_data=sd, company_name="NSTAGE", location="Hà Nội")

    proposal = _build_proposal(row, force=False)
    by_col = {fp.db_col: fp for fp in proposal.fields}

    assert by_col["company_name"].action == "SKIP_UNCHANGED"
    assert by_col["location"].action == "SKIP_UNCHANGED"


# ---------------------------------------------------------------------------
# EXTRA: evidence_text falls back to salaryRaw
# ---------------------------------------------------------------------------

def test_evidence_fallback_to_raw() -> None:
    sd = {"salaryMin": 20_000_000, "salaryRaw": "20 - 30 triệu/tháng"}
    excerpt = _evidence_text(sd, "salaryMin")
    assert "20 - 30" in excerpt or "20000000" in excerpt


# ---------------------------------------------------------------------------
# EXTRA: _run_parser smoke test (pure unit — no DB)
# ---------------------------------------------------------------------------

def test_run_parser_returns_nstage_company() -> None:
    """
    Call the real PR2 parser on NSTAGE raw text and verify it extracts
    companyName=NSTAGE and experienceMinYears=2.
    This validates the REEXTRACTED_FROM_RAW path.
    """
    result = _run_parser(NSTAGE_RAW_TEXT, "test-job-id")

    assert result.get("companyName") == "NSTAGE", (
        f"Parser should extract companyName='NSTAGE', got {result.get('companyName')!r}"
    )
    assert result.get("experienceMinYears") == 2, (
        f"Parser should extract experienceMinYears=2, got {result.get('experienceMinYears')!r}"
    )
    # seniority must stay None — no explicit mention in text
    assert result.get("seniority") is None
    # salary must stay None — no salary in text
    assert result.get("salaryMin") is None
    assert result.get("salaryNegotiable") is None


# ---------------------------------------------------------------------------
# EXTRA: _apply_proposal SQL correctness
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_apply_proposal_sql_correctness() -> None:
    sd = {"companyName": "NSTAGE", "location": "Hà Nội"}
    row = _make_row(job_id="jd-sql-test", structured_data=sd)
    proposal = _build_proposal(row, force=False)

    session = AsyncMock()
    n = await _apply_proposal(session, proposal)

    assert n == 2
    assert session.execute.call_count == 1
    assert session.commit.call_count == 1

    sql_str = str(session.execute.call_args[0][0])
    params = session.execute.call_args[0][1]

    assert "company_name = :company_name" in sql_str
    assert "location = :location" in sql_str
    assert "updated_at = now()" in sql_str
    assert params["company_name"] == "NSTAGE"
    assert params["location"] == "Hà Nội"
    assert params["id"] == "jd-sql-test"

    for forbidden in ("processing_status", "listing_status", "status", "structured_data"):
        assert forbidden not in params


# ---------------------------------------------------------------------------
# Requirement 1: structured_data IS NULL with raw_text present
# ---------------------------------------------------------------------------

def test_null_structured_data_with_raw_text_triggers_reextract() -> None:
    """
    When structured_data is NULL and raw_text has:
      "NStage : Fun Lives On"
      "Có ít nhất 2 năm kinh nghiệm"

    Expected:
      Record is inspected (not skipped)
      Proposal source = REEXTRACTED_FROM_RAW
      company_name = "NSTAGE"
      experience_min_years = 2
    """
    raw_text = (
        "NSTAGE : Fun Lives On\n"
        "Tuyển dụng Unity Developer\n"
        "Yêu cầu:\n"
        "- Có ít nhất 2 năm kinh nghiệm làm việc với Unity\n"
    )
    row = _make_row(
        job_id="jd-null-sd",
        title="Unity Developer",
        structured_data=None,  # structured_data IS NULL
        raw_text=raw_text,
    )

    proposal = _build_proposal(row, force=False)
    by_col = {fp.db_col: fp for fp in proposal.fields}

    # company_name
    assert by_col["company_name"].action == "UPDATE"
    assert by_col["company_name"].proposed_value == "NSTAGE"
    assert by_col["company_name"].proposal_source == REEXTRACTED_FROM_RAW

    # experience_min_years
    assert by_col["experience_min_years"].action == "UPDATE"
    assert by_col["experience_min_years"].proposed_value == 2
    assert by_col["experience_min_years"].proposal_source == REEXTRACTED_FROM_RAW


def test_candidate_query_allows_null_structured_data_if_raw_text_present() -> None:
    from scripts.backfill_job_listing_fields import _CANDIDATE_BASE
    assert "structured_data IS NOT NULL" in _CANDIDATE_BASE
    assert "raw_text IS NOT NULL" in _CANDIDATE_BASE
    assert "OR" in _CANDIDATE_BASE


