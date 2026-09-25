import pytest

from src.modules.job_descriptions.schemas import CanonicalJobDescription
from src.modules.matching.adapters import job_description_to_matching_job
from src.modules.matching.facade import MatchingFacade
from src.modules.matching.schemas import MatchRequest, SkillRequirement, UnresolvedRequirement
from tests.modules.matching.test_matching_pipeline import SHA256, StubEmbedder, _evidence, _payload


def _parsed_jd() -> CanonicalJobDescription:
    return CanonicalJobDescription.model_validate(
        {
            "schemaVersion": "1.0",
            "jobTitle": "Backend Developer",
            "seniority": "senior",
            "employmentType": "full_time",
            "workMode": "hybrid",
            "location": "Hà Nội",
            "responsibilities": [
                {"text": "Build reliable APIs", "evidenceRefs": ["jd-ev"]}
            ],
            "benefits": [{"text": "Learning budget", "evidenceRefs": ["jd-ev"]}],
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
                    "rawLabel": "3 years of Python",
                    "minimumExperienceMonths": 36,
                    "evidenceRefs": ["jd-ev"],
                },
                {
                    "requirementId": "req-degree",
                    "kind": "education",
                    "priority": "must_have",
                    "rawLabel": "Bachelor degree in Computer Science",
                    "evidenceRefs": ["jd-ev"],
                },
            ],
            "evidence": [_evidence("jd-ev", "jd-doc", "Build reliable APIs")],
            "parsing": {
                "parserVersion": "hybrid-jd-v1",
                "extractionVersion": "test",
                "parsedAt": "2026-09-12T00:00:00Z",
                "status": "ready",
            },
        }
    )


def test_adapter_preserves_existing_jd_fields_and_all_requirements() -> None:
    adapted = job_description_to_matching_job(_parsed_jd(), job_id="job-1")

    assert adapted.job_title == "Backend Developer"
    assert adapted.work_mode == "hybrid"
    assert adapted.location == "Hà Nội"
    assert adapted.responsibilities[0].text == "Build reliable APIs"
    assert adapted.benefits[0].text == "Learning budget"
    assert isinstance(adapted.requirements[0], SkillRequirement)
    assert adapted.requirements[0].operator == "gte"
    assert isinstance(adapted.requirements[1], UnresolvedRequirement)


def test_unresolved_must_have_is_not_silently_dropped_or_marked_met() -> None:
    payload = _payload(add_java=False)
    payload["resume"]["documentSha256"] = SHA256
    request = MatchRequest(
        schemaVersion="2.1",
        resume=payload["resume"],
        job=job_description_to_matching_job(_parsed_jd(), job_id="job-1"),
        asyncProcessing=False,
    )

    result = MatchingFacade(StubEmbedder()).match(request)

    degree = next(item for item in result.requirement_results if item.requirement_id == "req-degree")
    assert degree.status == "unknown"
    assert degree.reason_code == "raw_text_coverage_incomplete"
    assert degree.evidence_explanation
    assert result.eligibility == "review_required"
    assert result.decision == "abstained"


def test_adapter_fails_closed_when_finalized_jd_has_no_evidence() -> None:
    parsed = _parsed_jd().model_copy(update={"evidence": []})

    with pytest.raises(ValueError, match="document evidence"):
        job_description_to_matching_job(parsed, job_id="job-1")
