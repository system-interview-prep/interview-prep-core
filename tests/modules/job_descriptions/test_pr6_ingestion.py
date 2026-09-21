"""Tests for PR6: Real Job Ingestion (Greenhouse Adapter, Precedence, Sanitization, Dedup, Lifecycle).

Covers all mandatory invariants:
  A. New external job -> insert as ACTIVE with first_seen_at & last_seen_at
  B. Same external job -> update, không duplicate
  C. Missing optional fields -> null (no inference)
  D. Source_key identity đúng format (tenant:<board_token>) & check constraint compliance
  E. HTML sanitized (dangerous tags removed, structure converted to clean plain text)
  F. Parser nhận raw text (reusing existing deterministic parser)
  G. Source failure không fail cả batch (isolation of errors)
  H. Invalid URL reject/sanitize (javascript: rejected to null, https:// accepted)
  I. Re-fetch updates last_seen_at and fetched_at, preserves first_seen_at
  J. Human-reviewed value protection (crawler does not overwrite edited title/fields)
  K. Closed job lifecycle (absent jobs older than threshold transition to CLOSED)
  L. Listing API compatibility (row serialized properly by _job_description in router.py)
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.modules.job_descriptions.ingestion.adapters.greenhouse import (
    GreenhouseJobBoardAdapter,
    parse_iso_datetime,
)
from src.modules.job_descriptions.ingestion.models import (
    ExternalJobCandidate,
    IngestionConfig,
)
from src.modules.job_descriptions.ingestion.repository import JobIngestionRepository
from src.modules.job_descriptions.ingestion.sanitizer import HtmlSanitizer
from src.modules.job_descriptions.ingestion.service import JobIngestionService
from src.modules.job_descriptions.router import _job_description


# ---------------------------------------------------------------------------
# In-Memory Repository for Fast, Isolated Ingestion Testing
# ---------------------------------------------------------------------------

class InMemoryJobIngestionRepository:
    """Mock repository matching JobIngestionRepository contract for unit tests."""

    def __init__(self) -> None:
        self.jobs: dict[str, dict[str, Any]] = {}

    async def get_by_source_identity(
        self, source_type: str, source_key: str, external_job_id: str
    ) -> dict[str, Any] | None:
        for row in self.jobs.values():
            if (
                row.get("source_type") == source_type
                and row.get("source_key") == source_key
                and row.get("external_job_id") == external_job_id
            ):
                return dict(row)
        return None

    async def insert_candidate(
        self,
        candidate: ExternalJobCandidate,
        *,
        sanitized_html: str,
        plain_text: str,
        content_hash: str,
        parsed: Any | None,
        taxonomy_concept_id: str | None = None,
        taxonomy_version: str | None = "v1",
        now: datetime | None = None,
    ) -> str:
        job_id = f"job-{candidate.external_job_id}"
        now_dt = now or datetime.now(UTC)

        exp_min = parsed.experience_min_years if parsed else None
        exp_max = parsed.experience_max_years if parsed else None
        sal_min = parsed.salary_min if parsed else None
        sal_max = parsed.salary_max if parsed else None
        sal_curr = parsed.salary_currency if parsed else None
        sal_period = parsed.salary_period if parsed else None
        sal_neg = parsed.salary_negotiable if parsed else None
        work_mode = parsed.work_mode if parsed else None
        seniority = parsed.seniority if parsed else None

        extracted_metadata = {
            "ingestion": {
                "source_type": candidate.source_type,
                "source_key": candidate.source_key,
                "content_hash": content_hash,
                "fetched_at": now_dt.isoformat(),
                **candidate.metadata,
            },
            "raw_external_job": candidate.raw_payload,
            "is_human_reviewed": False,
        }

        self.jobs[job_id] = {
            "id": job_id,
            "title": candidate.title,
            "company_name": candidate.company_name,
            "company_logo_url": candidate.company_logo_url,
            "location": candidate.location,
            "work_mode": work_mode,
            "employment_type": None,
            "seniority": seniority,
            "experience_min_years": exp_min,
            "experience_max_years": exp_max,
            "salary_min": sal_min,
            "salary_max": sal_max,
            "salary_currency": sal_curr,
            "salary_period": sal_period,
            "salary_negotiable": sal_neg,
            "primary_taxonomy_concept_id": taxonomy_concept_id,
            "primary_taxonomy_version": taxonomy_version,
            "source_type": candidate.source_type,
            "source_key": candidate.source_key,
            "source_name": candidate.source_name,
            "source_url": candidate.source_url,
            "apply_url": candidate.apply_url,
            "external_job_id": candidate.external_job_id,
            "posted_at": candidate.posted_at,
            "first_seen_at": now_dt,
            "last_seen_at": now_dt,
            "fetched_at": now_dt,
            "listing_status": "ACTIVE",
            "processing_status": "DONE",
            "status": "ACTIVE",
            "description": sanitized_html,
            "raw_text": plain_text,
            "extracted_metadata": extracted_metadata,
            "structured_data": parsed.model_dump(by_alias=True) if parsed else None,
            "created_at": now_dt,
            "updated_at": now_dt,
        }
        return job_id

    async def update_candidate(
        self,
        existing_id: str,
        candidate: ExternalJobCandidate,
        *,
        sanitized_html: str,
        plain_text: str,
        content_hash: str,
        parsed: Any | None,
        is_human_reviewed: bool,
        taxonomy_concept_id: str | None = None,
        taxonomy_version: str | None = "v1",
        now: datetime | None = None,
    ) -> None:
        now_dt = now or datetime.now(UTC)
        row = self.jobs[existing_id]

        if is_human_reviewed:
            row["last_seen_at"] = now_dt
            row["fetched_at"] = now_dt
            if row.get("listing_status") in ("CLOSED", "EXPIRED"):
                row["listing_status"] = "ACTIVE"
            row["extracted_metadata"]["raw_external_job"] = candidate.raw_payload
            return

        exp_min = parsed.experience_min_years if parsed else None
        exp_max = parsed.experience_max_years if parsed else None
        work_mode = parsed.work_mode if parsed else None
        seniority = parsed.seniority if parsed else None

        row.update({
            "title": candidate.title,
            "company_name": candidate.company_name,
            "company_logo_url": candidate.company_logo_url,
            "location": candidate.location,
            "work_mode": work_mode,
            "seniority": seniority,
            "experience_min_years": exp_min,
            "experience_max_years": exp_max,
            "source_name": candidate.source_name,
            "source_url": candidate.source_url,
            "apply_url": candidate.apply_url,
            "posted_at": candidate.posted_at,
            "last_seen_at": now_dt,
            "fetched_at": now_dt,
            "listing_status": "ACTIVE",
            "description": sanitized_html,
            "raw_text": plain_text,
            "updated_at": now_dt,
        })

    async def touch_timestamps(self, existing_id: str, now: datetime | None = None) -> None:
        now_dt = now or datetime.now(UTC)
        row = self.jobs[existing_id]
        row["last_seen_at"] = now_dt
        row["fetched_at"] = now_dt
        if row.get("listing_status") in ("CLOSED", "EXPIRED"):
            row["listing_status"] = "ACTIVE"

    async def close_missing_jobs(
        self,
        source_type: str,
        source_key: str,
        seen_external_ids: list[str],
        *,
        threshold_dt: datetime,
    ) -> int:
        if not seen_external_ids:
            return 0
        closed = 0
        for row in self.jobs.values():
            if (
                row.get("source_type") == source_type
                and row.get("source_key") == source_key
                and row.get("listing_status") == "ACTIVE"
                and row.get("external_job_id") not in seen_external_ids
                and row.get("last_seen_at", datetime.max.replace(tzinfo=UTC)) < threshold_dt
            ):
                row["listing_status"] = "CLOSED"
                closed += 1
        return closed


# ---------------------------------------------------------------------------
# Test Suite
# ---------------------------------------------------------------------------

@pytest.fixture
def config() -> IngestionConfig:
    return IngestionConfig(
        board_token="stripe",
        company_name="Stripe",
        company_logo_url="https://images.stripe.com/logo.png",
    )


# [Test A] new external job -> insert as ACTIVE with first_seen_at & last_seen_at
@pytest.mark.asyncio
async def test_a_new_external_job_insert(config: IngestionConfig) -> None:
    repo = InMemoryJobIngestionRepository()
    service = JobIngestionService(repo)  # type: ignore[arg-type]

    candidates = [
        ExternalJobCandidate(
            external_job_id="101",
            title="Backend Engineer",
            company_name="Stripe",
            company_logo_url="https://images.stripe.com/logo.png",
            location="San Francisco, CA",
            raw_html="<p>We are looking for a Python developer with 3+ years experience.</p>",
            source_type="greenhouse",
            source_key="tenant:stripe",
            source_name="Stripe",
            source_url="https://boards.greenhouse.io/stripe/jobs/101",
            apply_url="https://boards.greenhouse.io/stripe/jobs/101",
            posted_at=datetime(2026, 3, 20, 10, 0, tzinfo=UTC),
        )
    ]

    summary = await service.ingest_candidates(config, candidates)

    assert summary.status == "COMPLETED"
    assert summary.metrics["created_count"] == 1
    assert summary.metrics["updated_count"] == 0

    job = await repo.get_by_source_identity("greenhouse", "tenant:stripe", "101")
    assert job is not None
    assert job["title"] == "Backend Engineer"
    assert job["company_name"] == "Stripe"
    assert job["location"] == "San Francisco, CA"
    assert job["listing_status"] == "ACTIVE"
    assert job["processing_status"] == "DONE"
    assert job["first_seen_at"] is not None
    assert job["last_seen_at"] is not None
    assert job["experience_min_years"] == 3


# [Test B] same external job -> update, không duplicate
@pytest.mark.asyncio
async def test_b_deduplication_no_duplicate_row(config: IngestionConfig) -> None:
    repo = InMemoryJobIngestionRepository()
    service = JobIngestionService(repo)  # type: ignore[arg-type]

    cand = ExternalJobCandidate(
        external_job_id="202",
        title="Frontend Engineer",
        company_name="Stripe",
        raw_html="<p>React and TypeScript developer.</p>",
        source_type="greenhouse",
        source_key="tenant:stripe",
    )

    t1 = datetime(2026, 3, 20, 10, 0, tzinfo=UTC)
    t2 = datetime(2026, 3, 21, 10, 0, tzinfo=UTC)

    # 1st Ingestion
    await service.ingest_candidates(config, [cand], now=t1)
    assert len(repo.jobs) == 1

    # 2nd Ingestion with exact same candidate
    summary2 = await service.ingest_candidates(config, [cand], now=t2)
    assert summary2.metrics["created_count"] == 0
    assert summary2.metrics["updated_count"] == 1
    assert summary2.metrics["unchanged_count"] == 1
    assert len(repo.jobs) == 1  # No duplicate row!

    job = await repo.get_by_source_identity("greenhouse", "tenant:stripe", "202")
    assert job["first_seen_at"] == t1
    assert job["last_seen_at"] == t2


# [Test C] missing optional fields -> null
@pytest.mark.asyncio
async def test_c_missing_optional_fields_null(config: IngestionConfig) -> None:
    repo = InMemoryJobIngestionRepository()
    service = JobIngestionService(repo)  # type: ignore[arg-type]

    cand = ExternalJobCandidate(
        external_job_id="303",
        title="General Manager",
        company_name="Stripe",
        location=None,  # missing location
        raw_html="<p>Responsible for regional operations.</p>",  # no salary, no exp
        source_type="greenhouse",
        source_key="tenant:stripe",
    )

    await service.ingest_candidates(config, [cand])
    job = await repo.get_by_source_identity("greenhouse", "tenant:stripe", "303")
    assert job is not None
    assert job["location"] is None
    assert job["salary_min"] is None
    assert job["salary_max"] is None
    assert job["salary_negotiable"] is None
    assert job["experience_min_years"] is None
    assert job["experience_max_years"] is None


# [Test D] source_key identity đúng format
def test_d_source_key_identity_format(config: IngestionConfig) -> None:
    adapter = GreenhouseJobBoardAdapter()
    item = {
        "id": 404,
        "title": "Data Analyst",
        "updated_at": "2026-03-20T12:00:00Z",
    }
    cand = adapter.parse_job_item(item, config)
    assert cand is not None
    assert cand.source_type == "greenhouse"
    assert cand.source_key == "tenant:stripe"
    assert cand.source_key != "default"
    assert cand.external_job_id == "404"


# [Test E] HTML sanitized
def test_e_html_sanitization() -> None:
    dirty_html = """
    <div>
        <script>alert('malicious')</script>
        <style>body { display: none; }</style>
        <h1>Job Title</h1>
        <p>Requirements:</p>
        <ul>
            <li>At least 2 years experience with Python</li>
            <li>Knowledge of SQL</li>
        </ul>
        <a href="javascript:stealCookie()">Click here</a>
    </div>
    """
    clean_plain = HtmlSanitizer.to_plain_text(dirty_html)
    assert "alert" not in clean_plain
    assert "style" not in clean_plain
    assert "Job Title" in clean_plain
    assert "At least 2 years experience with Python" in clean_plain
    assert "Knowledge of SQL" in clean_plain

    safe_html = HtmlSanitizer.sanitize_html(dirty_html)
    assert "<script" not in safe_html
    assert "<style" not in safe_html
    assert "javascript:" not in safe_html


# [Test F] parser nhận raw text
@pytest.mark.asyncio
async def test_f_parser_receives_raw_text(config: IngestionConfig) -> None:
    repo = InMemoryJobIngestionRepository()
    service = JobIngestionService(repo)  # type: ignore[arg-type]

    cand = ExternalJobCandidate(
        external_job_id="606",
        title="Senior Python Engineer",
        company_name="Stripe",
        raw_html="<p>Requirements: You must have at least 5 years of experience in Python and FastAPI.</p>",
        source_type="greenhouse",
        source_key="tenant:stripe",
    )

    await service.ingest_candidates(config, [cand])
    job = await repo.get_by_source_identity("greenhouse", "tenant:stripe", "606")
    assert job["experience_min_years"] == 5
    assert job["structured_data"] is not None


# [Test G] source failure không fail cả batch
@pytest.mark.asyncio
async def test_g_failure_isolation_per_candidate(config: IngestionConfig) -> None:
    repo = InMemoryJobIngestionRepository()
    service = JobIngestionService(repo)  # type: ignore[arg-type]

    cand_good = ExternalJobCandidate(
        external_job_id="701",
        title="Good Job",
        company_name="Stripe",
        source_key="tenant:stripe",
    )
    cand_bad = ExternalJobCandidate(
        external_job_id="702",
        title="Bad Job",
        company_name="Stripe",
        source_key="tenant:stripe",
    )

    # Patch repo to throw an error specifically on cand_bad
    orig_insert = repo.insert_candidate

    async def mock_insert(candidate: ExternalJobCandidate, **kwargs: Any) -> str:
        if candidate.external_job_id == "702":
            raise RuntimeError("Database deadlock simulation on job 702")
        return await orig_insert(candidate, **kwargs)

    repo.insert_candidate = mock_insert  # type: ignore[assignment]

    summary = await service.ingest_candidates(config, [cand_good, cand_bad])

    assert summary.status == "PARTIAL"
    assert summary.metrics["created_count"] == 1
    assert summary.metrics["failed_count"] == 1
    assert len(summary.errors) == 1
    assert "702" in summary.errors[0]

    # Good job was persisted successfully
    good = await repo.get_by_source_identity("greenhouse", "tenant:stripe", "701")
    assert good is not None


# [Test H] invalid URL reject/sanitize
def test_h_url_security_validation() -> None:
    assert HtmlSanitizer.validate_url("https://boards.greenhouse.io/stripe/jobs/123") == "https://boards.greenhouse.io/stripe/jobs/123"
    assert HtmlSanitizer.validate_url("http://example.com/apply") == "http://example.com/apply"
    assert HtmlSanitizer.validate_url("javascript:alert(1)") is None
    assert HtmlSanitizer.validate_url("data:text/html,<script>alert(1)</script>") is None
    assert HtmlSanitizer.validate_url("   ") is None
    assert HtmlSanitizer.validate_url(None) is None


# [Test I] re-fetch updates last_seen_at
@pytest.mark.asyncio
async def test_i_refetch_updates_last_seen_at(config: IngestionConfig) -> None:
    repo = InMemoryJobIngestionRepository()
    service = JobIngestionService(repo)  # type: ignore[arg-type]

    cand = ExternalJobCandidate(
        external_job_id="901",
        title="DevOps Engineer",
        company_name="Stripe",
        raw_html="<p>Kubernetes and Terraform.</p>",
        source_key="tenant:stripe",
    )

    t1 = datetime(2026, 3, 1, 10, 0, tzinfo=UTC)
    t2 = datetime(2026, 3, 5, 12, 0, tzinfo=UTC)

    await service.ingest_candidates(config, [cand], now=t1)
    await service.ingest_candidates(config, [cand], now=t2)

    job = await repo.get_by_source_identity("greenhouse", "tenant:stripe", "901")
    assert job["first_seen_at"] == t1
    assert job["last_seen_at"] == t2
    assert job["fetched_at"] == t2


# [Test J] human-reviewed value protection
@pytest.mark.asyncio
async def test_j_human_reviewed_fields_protected(config: IngestionConfig) -> None:
    repo = InMemoryJobIngestionRepository()
    service = JobIngestionService(repo)  # type: ignore[arg-type]

    cand = ExternalJobCandidate(
        external_job_id="1001",
        title="Original Crawler Title",
        company_name="Stripe",
        raw_html="<p>Content v1</p>",
        source_key="tenant:stripe",
    )

    # Initial crawl
    await service.ingest_candidates(config, [cand])

    # Admin reviews and edits the title
    job = await repo.get_by_source_identity("greenhouse", "tenant:stripe", "1001")
    job_id = job["id"]
    repo.jobs[job_id]["title"] = "Human Confirmed Senior Title"
    repo.jobs[job_id]["extracted_metadata"]["is_human_reviewed"] = True

    # Crawler runs again with modified external payload
    cand_v2 = ExternalJobCandidate(
        external_job_id="1001",
        title="Crawler Changed Title Again",
        company_name="Stripe",
        raw_html="<p>Content v2 modified</p>",
        source_key="tenant:stripe",
    )

    await service.ingest_candidates(config, [cand_v2])

    refetched = await repo.get_by_source_identity("greenhouse", "tenant:stripe", "1001")
    # Human confirmed title MUST be preserved!
    assert refetched["title"] == "Human Confirmed Senior Title"


# [Test K] closed job lifecycle
@pytest.mark.asyncio
async def test_k_closed_job_lifecycle_transition(config: IngestionConfig) -> None:
    repo = InMemoryJobIngestionRepository()
    service = JobIngestionService(repo)  # type: ignore[arg-type]

    t1 = datetime(2026, 3, 1, 10, 0, tzinfo=UTC)
    cand_a = ExternalJobCandidate(external_job_id="A", title="Job A", company_name="Stripe", source_key="tenant:stripe")
    cand_b = ExternalJobCandidate(external_job_id="B", title="Job B", company_name="Stripe", source_key="tenant:stripe")

    # Both jobs exist at t1
    await service.ingest_candidates(config, [cand_a, cand_b], now=t1)
    assert (await repo.get_by_source_identity("greenhouse", "tenant:stripe", "A"))["listing_status"] == "ACTIVE"
    assert (await repo.get_by_source_identity("greenhouse", "tenant:stripe", "B"))["listing_status"] == "ACTIVE"

    # 48 hours later (exceeds grace period of 24h), Job B is no longer on the board!
    t2 = t1 + timedelta(hours=48)
    summary2 = await service.ingest_candidates(config, [cand_a], now=t2)

    assert summary2.metrics["closed_count"] == 1

    job_a = await repo.get_by_source_identity("greenhouse", "tenant:stripe", "A")
    job_b = await repo.get_by_source_identity("greenhouse", "tenant:stripe", "B")

    assert job_a["listing_status"] == "ACTIVE"
    assert job_b["listing_status"] == "CLOSED"


# [Test L] listing API compatibility
def test_l_listing_api_compatibility() -> None:
    """Verifies that an ingested job row serializes cleanly through _job_description router function."""
    row = {
        "id": "ingested-job-uuid",
        "external_job_id": "999",
        "title": "Lead Software Engineer",
        "company_name": "Stripe",
        "company_logo_url": "https://stripe.com/logo.png",
        "location": "Remote, US",
        "work_mode": "remote",
        "employment_type": "full_time",
        "seniority": "lead",
        "experience_min_years": 7,
        "experience_max_years": None,
        "salary_min": 180000,
        "salary_max": 240000,
        "salary_currency": "USD",
        "salary_period": "year",
        "salary_negotiable": False,
        "primary_taxonomy_concept_id": "tech.backend",
        "primary_taxonomy_version": "v1",
        "taxonomy_label": "Backend Engineering",
        "taxonomy_kind": "role",
        "source_type": "greenhouse",
        "source_key": "tenant:stripe",
        "source_name": "Stripe",
        "source_url": "https://boards.greenhouse.io/stripe/jobs/999",
        "apply_url": "https://boards.greenhouse.io/stripe/jobs/999",
        "posted_at": datetime(2026, 3, 20, 10, 0, tzinfo=UTC),
        "listing_status": "ACTIVE",
        "processing_status": "DONE",
        "status": "ACTIVE",
        "keywords": ["python", "distributed-systems"],
        "description": "<p>Lead our payments team.</p>",
        "structured_data": {"skills": ["Python"]},
        "extracted_metadata": {"is_human_reviewed": False},
        "raw_text": "Lead our payments team.",
        "created_at": datetime(2026, 3, 20, 10, 0, tzinfo=UTC),
        "updated_at": datetime(2026, 3, 20, 10, 0, tzinfo=UTC),
    }

    serialized = _job_description(row)

    assert serialized["id"] == "ingested-job-uuid"
    assert serialized["externalJobId"] == "999"
    assert serialized["title"] == "Lead Software Engineer"
    assert serialized["company"] == {
        "name": "Stripe",
        "logoUrl": "https://stripe.com/logo.png",
    }
    assert serialized["location"] == "Remote, US"
    assert serialized["workMode"] == "remote"
    assert serialized["employmentType"] == "full_time"
    assert serialized["seniority"] == "lead"
    assert serialized["experience"] == {"minYears": 7, "maxYears": None}
    assert serialized["salary"] == {
        "min": 180000,
        "max": 240000,
        "currency": "USD",
        "period": "year",
        "negotiable": False,
    }
    assert serialized["primaryTaxonomy"] == {
        "version": "v1",
        "conceptId": "tech.backend",
        "label": "Backend Engineering",
        "kind": "role",
    }
    assert serialized["source"]["type"] == "greenhouse"
    assert serialized["source"]["name"] == "Stripe"
    assert serialized["source"]["applyUrl"] == "https://boards.greenhouse.io/stripe/jobs/999"
    assert serialized["listingStatus"] == "ACTIVE"
    assert serialized["processingStatus"] == "DONE"
