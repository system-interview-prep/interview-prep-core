import pytest
from pydantic import ValidationError

from src.modules.matching.facade import MatchingFacade
from src.modules.matching.schemas import StructuredMatchRequest


SHA256 = "a" * 64


def _evidence(evidence_id: str, document_id: str) -> dict:
    return {
        "evidenceId": evidence_id,
        "documentId": document_id,
        "documentSha256": SHA256,
        "section": "skills",
        "text": "0123456789",
        "charStart": 0,
        "charEnd": 10,
        "page": 1,
    }


def _payload(*, java_months: int | None = 48, add_java: bool = True) -> dict:
    skills = []
    if add_java:
        skills.append(
            {
                "claimId": "claim-java",
                "concept": {
                    "conceptId": "skill-java",
                    "scheme": "internal",
                    "taxonomyVersion": "2026.1",
                    "label": "Java",
                },
                "rawLabel": "Java",
                "experienceMonths": java_months,
                "evidenceRefs": ["cv-ev-java"],
            }
        )
    return {
        "schemaVersion": "2.1",
        "resume": {
            "schemaVersion": "2.1",
            "resumeId": "cv-1",
            "documentId": "cv-doc-1",
            "documentSha256": SHA256,
            "skills": skills,
            "evidence": [_evidence("cv-ev-java", "cv-doc-1")],
        },
        "job": {
            "schemaVersion": "2.1",
            "jobId": "job-1",
            "documentId": "job-doc-1",
            "documentSha256": SHA256,
            "evidence": [_evidence("jd-ev-java", "job-doc-1")],
            "requirements": [
                {
                    "requirementId": "req-java",
                    "type": "skill",
                    "priority": "must_have",
                    "sourceEvidenceRef": "jd-ev-java",
                    "skill": {
                        "conceptId": "skill-java",
                        "scheme": "internal",
                        "taxonomyVersion": "2026.1",
                        "label": "Java",
                    },
                    "operator": "gte",
                    "minimumExperienceMonths": 36,
                }
            ],
        },
        "matchingPolicy": {
            "policyVersion": "2026.1",
            "mustHaveMode": "strict",
            "unknownHandling": "manual_review",
        },
    }


def test_structured_match_marks_evidenced_must_have_as_met() -> None:
    result = MatchingFacade().match_structured(StructuredMatchRequest.model_validate(_payload()))
    assert result.overall_score == 1.0
    assert result.recommendation == "strong_match"
    assert result.requirement_results[0].status == "met"
    assert result.requirement_results[0].evidence_refs == ["cv-ev-java"]


def test_structured_match_rejects_below_minimum_must_have() -> None:
    result = MatchingFacade().match_structured(StructuredMatchRequest.model_validate(_payload(java_months=12)))
    assert result.recommendation == "not_match"
    assert result.requirement_results[0].status == "not_met"
    assert result.requirement_results[0].reason_code == "skill_duration_below_minimum"


def test_structured_match_keeps_missing_skill_as_unknown_and_review() -> None:
    result = MatchingFacade().match_structured(
        StructuredMatchRequest.model_validate(_payload(add_java=False))
    )
    assert result.recommendation == "review"
    assert result.requirement_results[0].status == "unknown"
    assert result.requirement_results[0].score is None


def test_structured_schema_rejects_invalid_requirement_constraint() -> None:
    payload = _payload()
    payload["job"]["requirements"][0].pop("minimumExperienceMonths")
    with pytest.raises(ValidationError, match="minimumExperienceMonths"):
        StructuredMatchRequest.model_validate(payload)
