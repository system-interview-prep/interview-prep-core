from copy import deepcopy
import pytest
from pydantic import ValidationError

from src.modules.job_descriptions.domain.schemas import CanonicalJobDescription


def _base_jd() -> dict:
    return {
        "schemaVersion": "1.0",
        "jobTitle": "Game Developer",
        "companyName": "NSTAGE",
        "workMode": "on_site",
        "employmentType": "full_time",
        "seniority": "senior",
        "location": "Hà Nội",
        "experienceMinYears": 2,
        "experienceMaxYears": 5,
        "salaryMin": 25000000,
        "salaryMax": 40000000,
        "salaryCurrency": "VND",
        "salaryPeriod": "month",
        "salaryNegotiable": False,
        "careerClassifications": [],
        "requirements": [],
        "responsibilities": [],
        "benefits": [],
        "evidence": [],
        "parsing": {
            "parserVersion": "deterministic-jd-v1",
            "extractionVersion": "mineru-test",
            "parsedAt": "2026-09-09T00:00:00Z",
            "status": "ready",
        },
    }


def test_canonical_jd_valid_with_new_fields() -> None:
    jd = CanonicalJobDescription.model_validate(_base_jd())
    assert jd.company_name == "NSTAGE"
    assert jd.work_mode == "on_site"
    assert jd.employment_type == "full_time"
    assert jd.seniority == "senior"
    assert jd.experience_min_years == 2
    assert jd.experience_max_years == 5
    assert jd.salary_min == 25000000
    assert jd.salary_max == 40000000
    assert jd.salary_currency == "VND"
    assert jd.salary_period == "month"
    assert jd.salary_negotiable is False


def test_canonical_jd_tri_state_salary_negotiable() -> None:
    # null = unknown
    payload = _base_jd()
    payload["salaryNegotiable"] = None
    jd = CanonicalJobDescription.model_validate(payload)
    assert jd.salary_negotiable is None

    # true = explicitly negotiable
    payload["salaryNegotiable"] = True
    jd = CanonicalJobDescription.model_validate(payload)
    assert jd.salary_negotiable is True


def test_canonical_jd_rejects_invalid_experience_range() -> None:
    payload = _base_jd()
    payload["experienceMinYears"] = 5
    payload["experienceMaxYears"] = 2
    with pytest.raises(ValidationError, match="experience_min_years cannot exceed experience_max_years"):
        CanonicalJobDescription.model_validate(payload)


def test_canonical_jd_rejects_invalid_salary_range() -> None:
    payload = _base_jd()
    payload["salaryMin"] = 50000000
    payload["salaryMax"] = 30000000
    with pytest.raises(ValidationError, match="salary_min cannot exceed salary_max"):
        CanonicalJobDescription.model_validate(payload)


def test_canonical_jd_rejects_negative_numeric_values() -> None:
    for field in ("experienceMinYears", "experienceMaxYears", "salaryMin", "salaryMax"):
        payload = _base_jd()
        payload[field] = -1
        with pytest.raises(ValidationError, match="greater than or equal to 0"):
            CanonicalJobDescription.model_validate(payload)


def test_canonical_jd_requires_currency_and_period_for_numeric_salary() -> None:
    payload = _base_jd()
    payload["salaryCurrency"] = None
    with pytest.raises(ValidationError, match="salary_currency is required"):
        CanonicalJobDescription.model_validate(payload)

    payload = _base_jd()
    payload["salaryPeriod"] = None
    with pytest.raises(ValidationError, match="salary_period is required"):
        CanonicalJobDescription.model_validate(payload)


def test_canonical_jd_rejects_invalid_enum_values() -> None:
    payload = _base_jd()
    payload["workMode"] = "onsite"  # must be on_site
    with pytest.raises(ValidationError):
        CanonicalJobDescription.model_validate(payload)

    payload = _base_jd()
    payload["seniority"] = "super_senior"
    with pytest.raises(ValidationError):
        CanonicalJobDescription.model_validate(payload)


def test_canonical_jd_rejects_lifecycle_and_identity_fields() -> None:
    # Requirement 5: CanonicalJobDescription must only contain extracted facts.
    # It must NOT contain source_type, source_key, external_job_id, status lifecycle fields.
    for field in ("source_type", "source_key", "external_job_id", "processing_status", "listing_status"):
        payload = deepcopy(_base_jd())
        payload[field] = "test"
        with pytest.raises(ValidationError, match="extra_forbidden|unexpected"):
            CanonicalJobDescription.model_validate(payload)
