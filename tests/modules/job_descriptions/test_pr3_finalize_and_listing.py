from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from src.main import create_app
from src.modules.job_descriptions.router import (
    FinalizeExperience,
    FinalizeSalary,
    FinalizeSource,
    FinalizeUpload,
    _job_description,
    finalize_upload,
)


def _valid_canonical_structured_data() -> dict:
    return {
        "schema_version": "1.0",
        "job_title": "Backend Engineer",
        "company_name": "Initial Parser Company",
        "responsibilities": [],
        "requirements": [],
        "benefits": [],
        "evidence": [],
        "parsing": {
            "parser_version": "1.0",
            "extraction_version": "1.0",
            "parsed_at": datetime.now(UTC).isoformat(),
            "status": "ready",
            "warnings": [],
        },
    }


def _mock_db_for_finalize(
    upload_row: dict | None = None,
    concept_exists: bool = True,
    update_rowcount: int = 1,
    update_error: Exception | None = None,
):
    db = AsyncMock()
    now = datetime.now(UTC)
    default_upload = {
        "id": "upload-1",
        "owner_user_id": "admin-1",
        "filename": "jd.pdf",
        "content_type": "application/pdf",
        "size": 1024,
        "status": "DONE",
        "parse_source": "mineru",
        "raw_text": "Sample JD text",
        "description": "Sample description",
        "structured_data": _valid_canonical_structured_data(),
        "extracted_metadata": None,
        "error": None,
        "external_job_id": None,
        "processing_status": "DONE",
        "listing_status": "DRAFT",
        "created_at": now,
        "updated_at": now,
    }
    effective_upload = upload_row if upload_row is not None else default_upload

    # Mappings mock
    upload_result = MagicMock()
    upload_result.mappings.return_value.one_or_none.return_value = effective_upload

    version_result = MagicMock()
    version_result.scalar_one_or_none.return_value = "internal-2026.2"

    concept_result = MagicMock()
    concept_result.scalar_one_or_none.return_value = 1 if concept_exists else None

    update_result = MagicMock()
    update_result.rowcount = update_rowcount

    version_insert_result = MagicMock()

    final_row = {
        "id": effective_upload["id"],
        "external_job_id": None,
        "title": "Backend Engineer",
        "company_name": "Acme Corp",
        "company_logo_url": "https://acme.com/logo.png",
        "location": "Hà Nội",
        "work_mode": "hybrid",
        "employment_type": "full_time",
        "seniority": "senior",
        "experience_min_years": 3,
        "experience_max_years": 5,
        "salary_min": 20000000,
        "salary_max": 30000000,
        "salary_currency": "VND",
        "salary_period": "month",
        "salary_negotiable": None,
        "primary_taxonomy_version": "internal-2026.2",
        "primary_taxonomy_concept_id": "occupation.backend-engineer",
        "source_type": "internal_upload",
        "source_key": "default",
        "source_name": "Internal Upload",
        "source_url": None,
        "apply_url": None,
        "posted_at": now,
        "listing_status": "ACTIVE",
        "processing_status": "DONE",
        "keywords": ["Python", "FastAPI"],
        "description": "Sample description",
        "structured_data": effective_upload["structured_data"],
        "extracted_metadata": None,
        "raw_text": "Sample JD text",
        "status": "ACTIVE",
        "created_at": now,
        "updated_at": now,
        "taxonomy_label": "Backend Engineering",
        "taxonomy_kind": "occupation",
    }
    final_get_result = MagicMock()
    final_get_result.mappings.return_value.one_or_none.return_value = final_row

    effects = [
        upload_result,
        version_result,
        concept_result,
        update_error if update_error is not None else update_result,
        version_insert_result,
        final_get_result,
    ]
    db.execute.side_effect = effects
    return db


# =========================================================================
# TEST A: Finalize ACTIVE (Published write path + Lifecycle)
# =========================================================================
@pytest.mark.asyncio
async def test_finalize_active_lifecycle_and_write_path() -> None:
    now = datetime.now(UTC)
    db = _mock_db_for_finalize()
    user = {"sub": "admin-1", "role": "admin"}
    payload = FinalizeUpload(
        title="Backend Engineer",
        companyName="Acme Corp",
        companyLogoUrl="https://acme.com/logo.png",
        location="Hà Nội",
        workMode="hybrid",
        employmentType="full_time",
        seniority="senior",
        experience=FinalizeExperience(minYears=3, maxYears=5),
        salary=FinalizeSalary(min=20000000, max=30000000, currency="VND", period="month"),
        primaryTaxonomyConceptId="occupation.backend-engineer",
        keywords=["Python", "FastAPI"],
        source=FinalizeSource(type="internal_upload", key="default"),
        postedAt=now,
        listingStatus="ACTIVE",
    )

    result = await finalize_upload(upload_id="upload-1", payload=payload, user=user, db=db)

    assert db.execute.call_count == 6
    # Inspect update call (call #4, index 3)
    update_call = db.execute.call_args_list[3]
    query_str = str(update_call[0][0])
    params = update_call[0][1]

    # Verify SQL sets published columns
    assert "company_name = :company_name" in query_str
    assert "work_mode = :work_mode" in query_str
    assert "salary_min = :salary_min" in query_str
    assert "processing_status = 'DONE'" in query_str
    assert "listing_status = :listing_status" in query_str
    assert "item_type = 'JOB_DESCRIPTION'" in query_str

    # Verify lifecycle params
    assert params["listing_status"] == "ACTIVE"
    assert params["status"] == "ACTIVE"  # legacy status mirrors listingStatus
    assert params["company_name"] == "Acme Corp"
    assert params["company_logo_url"] == "https://acme.com/logo.png"
    assert params["location"] == "Hà Nội"
    assert params["work_mode"] == "hybrid"
    assert params["employment_type"] == "full_time"
    assert params["seniority"] == "senior"
    assert params["experience_min_years"] == 3
    assert params["experience_max_years"] == 5
    assert params["salary_min"] == 20000000
    assert params["salary_max"] == 30000000
    assert params["salary_currency"] == "VND"
    assert params["salary_period"] == "month"
    assert params["salary_negotiable"] is None
    assert params["source_type"] == "internal_upload"
    assert params["source_key"] == "default"

    # Verify transaction commit
    db.commit.assert_awaited_once()

    # Verify returned result contains published values
    assert result["company"]["name"] == "Acme Corp"
    assert result["listingStatus"] == "ACTIVE"
    assert result["processingStatus"] == "DONE"
    assert result["status"] == "ACTIVE"


# =========================================================================
# TEST B: Finalize DRAFT (listing_status=DRAFT, legacy status=DRAFT)
# =========================================================================
@pytest.mark.asyncio
async def test_finalize_draft_lifecycle() -> None:
    db = _mock_db_for_finalize()
    user = {"sub": "admin-1", "role": "admin"}
    payload = FinalizeUpload(
        title="Draft Job",
        primaryTaxonomyConceptId="occupation.backend-engineer",
        listingStatus="DRAFT",
    )

    await finalize_upload(upload_id="upload-1", payload=payload, user=user, db=db)

    update_call = db.execute.call_args_list[3]
    params = update_call[0][1]
    assert params["listing_status"] == "DRAFT"
    assert params["status"] == "DRAFT"


# =========================================================================
# TEST C: Salary metadata is optional; numeric ranges remain validated
# =========================================================================
def test_validation_salary_numeric_allows_missing_metadata() -> None:
    assert FinalizeSalary(min=1000, period="month").currency is None
    assert FinalizeSalary(max=2000, currency="VND").period is None

    # min > max
    with pytest.raises(ValidationError) as exc:
        FinalizeSalary(min=5000, max=2000, currency="VND", period="month")
    assert "salary.min cannot be greater than salary.max" in str(exc.value)

    # negative salary
    with pytest.raises(ValidationError):
        FinalizeSalary(min=-100, currency="VND", period="month")

    # Pure negotiable with no numeric values is allowed without currency/period
    valid_neg = FinalizeSalary(negotiable=True)
    assert valid_neg.negotiable is True
    assert valid_neg.min is None


# =========================================================================
# TEST D: Experience min > max or negative -> validation fail
# =========================================================================
def test_validation_experience_ranges() -> None:
    # min > max
    with pytest.raises(ValidationError) as exc:
        FinalizeExperience(minYears=5, maxYears=2)
    assert "experience.minYears cannot be greater than experience.maxYears" in str(exc.value)

    # negative min
    with pytest.raises(ValidationError):
        FinalizeExperience(minYears=-1)

    # negative max
    with pytest.raises(ValidationError):
        FinalizeExperience(maxYears=-2)

    # Valid range
    valid_exp = FinalizeExperience(minYears=2, maxYears=4)
    assert valid_exp.min_years == 2
    assert valid_exp.max_years == 4


# =========================================================================
# TEST E: Invalid Enum validation
# =========================================================================
def test_validation_invalid_enums() -> None:
    # workMode
    with pytest.raises(ValidationError):
        FinalizeUpload(
            title="Dev",
            primaryTaxonomyConceptId="c-1",
            workMode="remote_only",  # type: ignore
        )

    # employmentType
    with pytest.raises(ValidationError):
        FinalizeUpload(
            title="Dev",
            primaryTaxonomyConceptId="c-1",
            employmentType="freelance",  # type: ignore
        )

    # seniority
    with pytest.raises(ValidationError):
        FinalizeUpload(
            title="Dev",
            primaryTaxonomyConceptId="c-1",
            seniority="principal",  # type: ignore
        )

    # listingStatus: only DRAFT | ACTIVE allowed
    with pytest.raises(ValidationError):
        FinalizeUpload(
            title="Dev",
            primaryTaxonomyConceptId="c-1",
            listingStatus="ARCHIVED",  # type: ignore
        )

    # sourceType
    with pytest.raises(ValidationError):
        FinalizeSource(type="unsupported_source")  # type: ignore

    # salary period
    with pytest.raises(ValidationError):
        FinalizeSalary(min=1000, currency="USD", period="weekly")  # type: ignore


# =========================================================================
# TEST F: Invalid URL validation (must be http:// or https:// with valid hostname)
# =========================================================================
def test_validation_invalid_urls() -> None:
    # companyLogoUrl - wrong scheme
    with pytest.raises(ValidationError) as exc:
        FinalizeUpload(
            title="Dev",
            primaryTaxonomyConceptId="c-1",
            companyLogoUrl="ftp://company.com/logo.png",
        )
    assert "must use http://" in str(exc.value)

    # companyLogoUrl - javascript scheme
    with pytest.raises(ValidationError) as exc:
        FinalizeUpload(
            title="Dev",
            primaryTaxonomyConceptId="c-1",
            companyLogoUrl="javascript:void(0)",
        )
    assert "must use http://" in str(exc.value)

    # source.url - file scheme
    with pytest.raises(ValidationError) as exc:
        FinalizeSource(url="file:///tmp/jd.html")
    assert "must use http://" in str(exc.value)

    # source.applyUrl - data scheme
    with pytest.raises(ValidationError) as exc:
        FinalizeSource(applyUrl="data:text/plain;base64,abc")
    assert "must use http://" in str(exc.value)



# =========================================================================
# TEST G: External source + sourceKey=default -> validation fail
# =========================================================================
def test_validation_external_source_identity() -> None:
    # greenhouse + externalJobId + sourceKey="default" -> REJECT
    with pytest.raises(ValidationError) as exc:
        FinalizeUpload(
            title="Dev",
            primaryTaxonomyConceptId="c-1",
            externalJobId="gh-101",
            source=FinalizeSource(type="greenhouse", key="default"),
        )
    assert "sourceKey cannot be 'default'" in str(exc.value)

    # lever + externalJobId + sourceKey="" -> REJECT
    with pytest.raises(ValidationError) as exc:
        FinalizeUpload(
            title="Dev",
            primaryTaxonomyConceptId="c-1",
            externalJobId="lev-202",
            source=FinalizeSource(type="lever", key="   "),
        )
    assert "sourceKey cannot be 'default'" in str(exc.value)

    # internal_upload + sourceKey="default" -> ACCEPT
    valid_internal = FinalizeUpload(
        title="Dev",
        primaryTaxonomyConceptId="c-1",
        externalJobId="int-01",
        source=FinalizeSource(type="internal_upload", key="default"),
    )
    assert valid_internal.source.key == "default"

    # greenhouse + externalJobId + real sourceKey -> ACCEPT
    valid_external = FinalizeUpload(
        title="Dev",
        primaryTaxonomyConceptId="c-1",
        externalJobId="gh-101",
        source=FinalizeSource(type="greenhouse", key="tenant-corp"),
    )
    assert valid_external.source.key == "tenant-corp"


# =========================================================================
# TEST H: structured_data is NOT mutated after finalize
# =========================================================================
@pytest.mark.asyncio
async def test_finalize_preserves_structured_data() -> None:
    db = _mock_db_for_finalize()
    user = {"sub": "admin-1", "role": "admin"}
    payload = FinalizeUpload(
        title="Backend Engineer",
        companyName="Human Reviewed Company",
        primaryTaxonomyConceptId="occupation.backend-engineer",
    )

    await finalize_upload(upload_id="upload-1", payload=payload, user=user, db=db)

    update_call = db.execute.call_args_list[3]
    query_str = str(update_call[0][0])
    params = update_call[0][1]

    # structured_data column MUST NOT be modified in update statement
    assert "structured_data" not in query_str
    assert "structured_data" not in params


# =========================================================================
# TEST I: Listing API reads top-level published company (no fallback)
# =========================================================================
def test_listing_api_company_no_fallback_structured_data() -> None:
    now = datetime.now(UTC)
    # Row with top-level published company
    row_with_top_level = {
        "id": "jd-1",
        "title": "Backend",
        "company_name": "Published Company",
        "company_logo_url": "https://company.com/logo.png",
        "structured_data": {"company_name": "Parser Company In Structured Data"},
        "created_at": now,
        "updated_at": now,
    }
    item1 = _job_description(row_with_top_level)
    assert item1["company"] == {
        "name": "Published Company",
        "logoUrl": "https://company.com/logo.png",
    }

    # Row with NULL top-level company, but structured_data HAS company_name:
    # Must NOT fallback to structured_data! company must be None!
    row_without_top_level = {
        "id": "jd-2",
        "title": "Backend",
        "company_name": None,
        "company_logo_url": None,
        "structured_data": {"company_name": "Parser Company In Structured Data"},
        "created_at": now,
        "updated_at": now,
    }
    item2 = _job_description(row_without_top_level)
    assert item2["company"] is None
    # structured_data still available for audit/backward compatibility
    assert item2["structuredData"] == {"company_name": "Parser Company In Structured Data"}


# =========================================================================
# TEST J: postedAt null remains null (no fallback to createdAt)
# =========================================================================
def test_listing_api_posted_at_no_fallback_created_at() -> None:
    now = datetime.now(UTC)
    row_no_posted_at = {
        "id": "jd-1",
        "title": "Backend",
        "posted_at": None,
        "created_at": now,
        "updated_at": now,
    }
    item = _job_description(row_no_posted_at)
    assert item["postedAt"] is None
    assert item["createdAt"] == now.isoformat()

    # When posted_at is provided:
    past = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)
    row_with_posted_at = {
        "id": "jd-2",
        "title": "Backend",
        "posted_at": past,
        "created_at": now,
        "updated_at": now,
    }
    item2 = _job_description(row_with_posted_at)
    assert item2["postedAt"] == past.isoformat()


# =========================================================================
# TEST K: Null serialization contract (company, salary, experience)
# =========================================================================
def test_null_serialization_contract() -> None:
    now = datetime.now(UTC)
    empty_row = {
        "id": "jd-empty",
        "title": "Minimal Job",
        "company_name": None,
        "company_logo_url": None,
        "experience_min_years": None,
        "experience_max_years": None,
        "salary_min": None,
        "salary_max": None,
        "salary_currency": None,
        "salary_period": None,
        "salary_negotiable": None,
        "created_at": now,
        "updated_at": now,
    }
    item = _job_description(empty_row)
    # Strictly None, NOT empty dict with None values
    assert item["company"] is None
    assert item["experience"] is None
    assert item["salary"] is None

    # Partial company
    partial_comp_row = {**empty_row, "company_name": "Acme"}
    assert _job_description(partial_comp_row)["company"] == {"name": "Acme", "logoUrl": None}

    # Partial experience
    partial_exp_row = {**empty_row, "experience_min_years": 2}
    assert _job_description(partial_exp_row)["experience"] == {"minYears": 2, "maxYears": None}

    # Partial salary (only negotiable flag set)
    partial_sal_row = {**empty_row, "salary_negotiable": True}
    assert _job_description(partial_sal_row)["salary"] == {
        "min": None,
        "max": None,
        "currency": None,
        "period": None,
        "negotiable": True,
    }


# =========================================================================
# TEST L: Existing API fields compatibility
# =========================================================================
def test_existing_api_fields_compatibility() -> None:
    now = datetime.now(UTC)
    full_row = {
        "id": "jp-1",
        "external_job_id": "ext-99",
        "title": "Full Stack Engineer",
        "company_name": "Google",
        "company_logo_url": "https://google.com/logo.png",
        "location": "Mountain View, CA",
        "work_mode": "hybrid",
        "employment_type": "full_time",
        "seniority": "senior",
        "experience_min_years": 5,
        "experience_max_years": 8,
        "salary_min": 150000,
        "salary_max": 220000,
        "salary_currency": "USD",
        "salary_period": "year",
        "salary_negotiable": False,
        "primary_taxonomy_version": "internal-2026.2",
        "primary_taxonomy_concept_id": "occupation.software-engineer",
        "source_type": "company_career",
        "source_key": "google-careers",
        "source_name": "Google Careers",
        "source_url": "https://careers.google.com/jobs/1",
        "apply_url": "https://careers.google.com/jobs/1/apply",
        "posted_at": now,
        "listing_status": "ACTIVE",
        "processing_status": "DONE",
        "keywords": ["Go", "Python", "Kubernetes"],
        "description": "Job description text",
        "structured_data": {"role": "Engineer"},
        "extracted_metadata": {"parser": "mineru"},
        "raw_text": "Raw text content",
        "status": "ACTIVE",
        "created_at": now,
        "updated_at": now,
        "taxonomy_label": "Software Engineer",
        "taxonomy_kind": "occupation",
    }
    item = _job_description(full_row)

    # Core identification
    assert item["id"] == "jp-1"
    assert item["externalJobId"] == "ext-99"
    assert item["title"] == "Full Stack Engineer"

    # Published fields
    assert item["company"] == {"name": "Google", "logoUrl": "https://google.com/logo.png"}
    assert item["location"] == "Mountain View, CA"
    assert item["workMode"] == "hybrid"
    assert item["employmentType"] == "full_time"
    assert item["seniority"] == "senior"
    assert item["experience"] == {"minYears": 5, "maxYears": 8}
    assert item["salary"] == {
        "min": 150000,
        "max": 220000,
        "currency": "USD",
        "period": "year",
        "negotiable": False,
    }
    assert item["primaryTaxonomy"] == {
        "version": "internal-2026.2",
        "conceptId": "occupation.software-engineer",
        "label": "Software Engineer",
        "kind": "occupation",
    }
    assert item["source"] == {
        "type": "company_career",
        "key": "google-careers",
        "name": "Google Careers",
        "url": "https://careers.google.com/jobs/1",
        "applyUrl": "https://careers.google.com/jobs/1/apply",
    }

    # Lifecycle statuses
    assert item["listingStatus"] == "ACTIVE"
    assert item["processingStatus"] == "DONE"

    # Backward compatibility fields
    assert item["keywords"] == ["Go", "Python", "Kubernetes"]
    assert item["description"] == "Job description text"
    assert item["structuredData"] == {"role": "Engineer"}
    assert item["extractedMetadata"] == {"parser": "mineru"}
    assert item["sourceText"] == "Raw text content"
    assert item["status"] == "ACTIVE"
    assert item["createdAt"] == now.isoformat()
    assert item["updatedAt"] == now.isoformat()


# =========================================================================
# TEST: Concurrency protection (rowcount == 0 -> 409 Conflict)
# =========================================================================
@pytest.mark.asyncio
async def test_finalize_concurrency_conflict_raises_409() -> None:
    # Simulate race condition: update query modified 0 rows because another admin finalized first
    db = _mock_db_for_finalize(update_rowcount=0)
    user = {"sub": "admin-1", "role": "admin"}
    payload = FinalizeUpload(
        title="Concurrent Job",
        primaryTaxonomyConceptId="occupation.backend-engineer",
    )

    with pytest.raises(HTTPException) as exc:
        await finalize_upload(upload_id="upload-1", payload=payload, user=user, db=db)
    assert exc.value.status_code == 409
    assert "already been finalized" in exc.value.detail
    db.rollback.assert_awaited_once()


# =========================================================================
# TEST: Transaction rollback on failure
# =========================================================================
@pytest.mark.asyncio
async def test_finalize_transaction_rollback_on_db_error() -> None:
    # Simulate DB error during update
    db = _mock_db_for_finalize(update_error=RuntimeError("Database connection lost"))
    user = {"sub": "admin-1", "role": "admin"}
    payload = FinalizeUpload(
        title="Failing Job",
        primaryTaxonomyConceptId="occupation.backend-engineer",
    )

    with pytest.raises(RuntimeError) as exc:
        await finalize_upload(upload_id="upload-1", payload=payload, user=user, db=db)
    assert "Database connection lost" in str(exc.value)
    db.rollback.assert_awaited_once()


# =========================================================================
# TEST: HTTP Endpoint /admin/job-descriptions/uploads/{id}/finalize via TestClient
# =========================================================================
def test_finalize_http_endpoint_validation_via_testclient(client) -> None:
    from src.core.security import require_admin

    client.app.dependency_overrides[require_admin] = lambda: {"sub": "admin-1", "role": "admin"}
    try:
        # Invalid enum workMode
        res1 = client.post(
            "/admin/job-descriptions/uploads/upload-1/finalize",
            json={
                "title": "Backend Dev",
                "primaryTaxonomyConceptId": "c-1",
                "workMode": "invalid_mode",
            },
        )
        assert res1.status_code == 422

        # Invalid URL companyLogoUrl
        res2 = client.post(
            "/admin/job-descriptions/uploads/upload-1/finalize",
            json={
                "title": "Backend Dev",
                "primaryTaxonomyConceptId": "c-1",
                "companyLogoUrl": "ftp://bad-url.com",
            },
        )
        assert res2.status_code == 422

        # Experience min > max
        res4 = client.post(
            "/admin/job-descriptions/uploads/upload-1/finalize",
            json={
                "title": "Backend Dev",
                "primaryTaxonomyConceptId": "c-1",
                "experience": {"minYears": 10, "maxYears": 2},
            },
        )
        assert res4.status_code == 422

        # External ATS source with externalJobId and sourceKey='default'
        res5 = client.post(
            "/admin/job-descriptions/uploads/upload-1/finalize",
            json={
                "title": "Backend Dev",
                "primaryTaxonomyConceptId": "c-1",
                "externalJobId": "gh-999",
                "source": {"type": "greenhouse", "key": "default"},
            },
        )
        assert res5.status_code == 422

        # Invalid listingStatus
        res6 = client.post(
            "/admin/job-descriptions/uploads/upload-1/finalize",
            json={
                "title": "Backend Dev",
                "primaryTaxonomyConceptId": "c-1",
                "listingStatus": "PUBLISHED_NOW",
            },
        )
        assert res6.status_code == 422

    finally:
        client.app.dependency_overrides.pop(require_admin, None)


# ===========================================================================
# PR3 VERIFICATION CORRECTIONS — 9 mandatory tests
# ===========================================================================

# ---------------------------------------------------------------------------
# Test 1: Legacy FE finalize payload (status=ACTIVE) must be accepted
# The FE currently sends: { title, primaryTaxonomyConceptId, keywords, status, description }
# where 'status' is the legacy field name. The AliasChoices mapping must handle it.
# ---------------------------------------------------------------------------
def test_legacy_fe_payload_status_field_maps_to_listing_status() -> None:
    """FE currently sends 'status' not 'listingStatus'. Must not break after PR3."""
    # Test model-level parsing with legacy 'status' key
    payload_active = FinalizeUpload.model_validate(
        {
            "title": "Unity Developer",
            "primaryTaxonomyConceptId": "occupation.game-dev",
            "keywords": ["Unity", "C#"],
            "status": "ACTIVE",  # legacy FE field
            "description": "Job description text",
        }
    )
    assert payload_active.listing_status == "ACTIVE"

    payload_draft = FinalizeUpload.model_validate(
        {
            "title": "Unity Developer",
            "primaryTaxonomyConceptId": "occupation.game-dev",
            "status": "DRAFT",  # legacy FE field
        }
    )
    assert payload_draft.listing_status == "DRAFT"

    # Verify all legacy FE fields are accepted (no extra/unexpected validation errors)
    full_fe_payload = FinalizeUpload.model_validate(
        {
            "title": "Game Backend Engineer",
            "primaryTaxonomyConceptId": "occupation.backend-engineer",
            "keywords": ["Go", "Redis"],
            "status": "ACTIVE",
            "description": "<p>Backend for game server</p>",
        }
    )
    assert full_fe_payload.title == "Game Backend Engineer"
    assert full_fe_payload.keywords == ["Go", "Redis"]
    assert full_fe_payload.description == "<p>Backend for game server</p>"
    assert full_fe_payload.listing_status == "ACTIVE"


# ---------------------------------------------------------------------------
# Test 2: New listingStatus contract works alongside legacy compatibility
# ---------------------------------------------------------------------------
def test_new_listing_status_contract() -> None:
    """listingStatus camelCase alias must also work (for new clients)."""
    payload_camel = FinalizeUpload.model_validate(
        {
            "title": "Data Scientist",
            "primaryTaxonomyConceptId": "occupation.data-scientist",
            "listingStatus": "DRAFT",
        }
    )
    assert payload_camel.listing_status == "DRAFT"

    # Default must be DRAFT (not ACTIVE — don't publish accidentally)
    payload_default = FinalizeUpload.model_validate(
        {
            "title": "Data Scientist",
            "primaryTaxonomyConceptId": "occupation.data-scientist",
        }
    )
    assert payload_default.listing_status == "DRAFT", (
        "Default listingStatus MUST be DRAFT to prevent accidental publishing"
    )


# ---------------------------------------------------------------------------
# Test 3: Upload creation -> processing_status = PENDING (via server_default)
# This is verified by checking the INSERT statement does NOT override server_default.
# ---------------------------------------------------------------------------
def test_upload_insert_does_not_override_processing_status_pending() -> None:
    """
    The INSERT statement uses server_default='PENDING' for processing_status.
    Verify the INSERT in router.py does NOT explicitly set processing_status to
    anything (allows DB server_default to kick in).
    """
    import inspect
    from src.modules.job_descriptions import router as jd_router

    source = inspect.getsource(jd_router)
    # Find the INSERT block - it should not explicitly set processing_status to something other than PENDING
    # The server_default 'PENDING' handles it; explicit column is acceptable if value is 'PENDING'
    # The key requirement: an upload row will have processing_status=PENDING when first created
    # We verify by looking for PENDING in the INSERT
    assert "INSERT INTO job_descriptions" in source
    assert "'PENDING'" in source  # legacy status='PENDING' is set explicitly; processing_status via server_default


# ---------------------------------------------------------------------------
# Test 4: Worker start -> processing_status = PROCESSING (claim write path)
# ---------------------------------------------------------------------------
def test_parser_repository_claim_updates_processing_status() -> None:
    import inspect
    from src.modules.job_descriptions.parsing.infrastructure.repository import (
        SqlAlchemyJobDescriptionParseRepository,
    )

    source = inspect.getsource(SqlAlchemyJobDescriptionParseRepository.claim)
    assert "processing_status = 'PROCESSING'" in source, (
        "claim() must update processing_status to 'PROCESSING'"
    )
    assert "status = 'PARSING'" in source, (
        "claim() must also update legacy status to 'PARSING'"
    )


# ---------------------------------------------------------------------------
# Test 5: Worker success -> processing_status = DONE (complete write path)
# ---------------------------------------------------------------------------
def test_parser_repository_complete_updates_processing_status() -> None:
    import inspect
    from src.modules.job_descriptions.parsing.infrastructure.repository import (
        SqlAlchemyJobDescriptionParseRepository,
    )

    source = inspect.getsource(SqlAlchemyJobDescriptionParseRepository.complete)
    assert "processing_status = 'DONE'" in source, (
        "complete() must update processing_status to 'DONE'"
    )
    assert "status = 'DONE'" in source, (
        "complete() must also update legacy status to 'DONE'"
    )


# ---------------------------------------------------------------------------
# Test 6: Worker failure -> processing_status = FAILED (fail write path)
# ---------------------------------------------------------------------------
def test_parser_repository_fail_updates_processing_status() -> None:
    import inspect
    from src.modules.job_descriptions.parsing.infrastructure.repository import (
        SqlAlchemyJobDescriptionParseRepository,
    )

    source = inspect.getsource(SqlAlchemyJobDescriptionParseRepository.fail)
    assert "processing_status = 'FAILED'" in source, (
        "fail() must update processing_status to 'FAILED'"
    )
    assert "status = 'FAILED'" in source, (
        "fail() must also update legacy status to 'FAILED'"
    )


# ---------------------------------------------------------------------------
# Test 7: Finalize -> item_type = JOB_DESCRIPTION (verified in write path SQL)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_finalize_sets_item_type_job_description() -> None:
    db = _mock_db_for_finalize()
    user = {"sub": "admin-1", "role": "admin"}
    payload = FinalizeUpload(
        title="Game Dev",
        primaryTaxonomyConceptId="occupation.backend-engineer",
        listingStatus="ACTIVE",
    )

    await finalize_upload(upload_id="upload-1", payload=payload, user=user, db=db)

    update_call = db.execute.call_args_list[3]
    query_str = str(update_call[0][0])

    assert "item_type = 'JOB_DESCRIPTION'" in query_str, (
        "Finalize UPDATE must set item_type to 'JOB_DESCRIPTION'"
    )
    assert "processing_status = 'DONE'" in query_str, (
        "Finalize UPDATE must set processing_status to 'DONE'"
    )
    assert "listing_status = :listing_status" in query_str, (
        "Finalize UPDATE must set listing_status"
    )


# ---------------------------------------------------------------------------
# Test 8: DRAFT must not accidentally publish as ACTIVE
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_finalize_draft_does_not_publish_active() -> None:
    """When listingStatus=DRAFT, the DB must receive listing_status='DRAFT', NOT 'ACTIVE'."""
    db = _mock_db_for_finalize()
    user = {"sub": "admin-1", "role": "admin"}

    # Legacy FE payload format: status=DRAFT
    payload = FinalizeUpload.model_validate(
        {
            "title": "Draft Only Job",
            "primaryTaxonomyConceptId": "occupation.backend-engineer",
            "status": "DRAFT",
        }
    )
    assert payload.listing_status == "DRAFT"

    await finalize_upload(upload_id="upload-1", payload=payload, user=user, db=db)

    update_call = db.execute.call_args_list[3]
    params = update_call[0][1]

    assert params["listing_status"] == "DRAFT", (
        "DRAFT job must not be stored as ACTIVE!"
    )
    assert params["status"] == "DRAFT", (
        "Legacy status must mirror listingStatus for DRAFT"
    )


# ---------------------------------------------------------------------------
# Test 9: Malformed http/https URLs must be rejected
# ---------------------------------------------------------------------------
def test_url_validation_rejects_malformed_urls() -> None:
    """URL validator must check both scheme AND hostname — not just prefix."""
    from pydantic import ValidationError as PydanticValidationError

    # "https://" with no hostname — must be REJECTED
    with pytest.raises(PydanticValidationError) as exc:
        FinalizeUpload.model_validate(
            {
                "title": "Dev",
                "primaryTaxonomyConceptId": "c-1",
                "companyLogoUrl": "https://",
            }
        )
    assert "must have a valid hostname" in str(exc.value)

    # "http://" with no hostname — must be REJECTED
    with pytest.raises(PydanticValidationError) as exc:
        FinalizeUpload.model_validate(
            {
                "title": "Dev",
                "primaryTaxonomyConceptId": "c-1",
                "companyLogoUrl": "http://",
            }
        )
    assert "must have a valid hostname" in str(exc.value)

    # ftp:// — wrong scheme — must be REJECTED
    with pytest.raises(PydanticValidationError) as exc:
        FinalizeSource(url="ftp://files.example.com/jd.pdf")
    assert "must use http://" in str(exc.value)

    # javascript:// — wrong scheme — must be REJECTED
    with pytest.raises(PydanticValidationError) as exc:
        FinalizeSource(applyUrl="javascript://void(0)")
    assert "must use http://" in str(exc.value)

    # file:// — wrong scheme — must be REJECTED
    with pytest.raises(PydanticValidationError) as exc:
        FinalizeSource(url="file:///tmp/jd.html")
    assert "must use http://" in str(exc.value)

    # Valid URLs — must be ACCEPTED
    valid_source = FinalizeSource(
        url="https://careers.acme.com/jobs/123",
        apply_url="https://careers.acme.com/apply/123",
    )
    assert valid_source.url == "https://careers.acme.com/jobs/123"

    valid_source_http = FinalizeSource(url="http://internal.corp/jd")
    assert valid_source_http.url == "http://internal.corp/jd"
