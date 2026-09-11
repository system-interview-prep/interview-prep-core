from copy import deepcopy

import pytest
from pydantic import ValidationError

from src.modules.job_descriptions.domain.schemas import CanonicalJobDescription


def _valid_jd() -> dict:
    return {
        "schemaVersion": "1.0",
        "jobTitle": "Backend Engineer",
        "careerClassifications": [
            {
                "code": "technology.software-engineering.backend",
                "label": "Backend Engineering",
                "dimension": "specialization",
                "taxonomyVersion": "internal-career-2026.1",
                "isPrimary": True,
                "confidence": 0.7,
                "evidenceRefs": ["ev-1"],
            }
        ],
        "requirements": [
            {
                "requirementId": "req-java",
                "kind": "skill",
                "priority": "must_have",
                "concept": {
                    "conceptId": "skill-java",
                    "scheme": "internal",
                    "taxonomyVersion": "internal-2026.1",
                    "label": "Java",
                },
                "rawLabel": "Java",
                "minimumExperienceMonths": 24,
                "evidenceRefs": ["ev-1"],
            }
        ],
        "responsibilities": [{"text": "Build APIs", "evidenceRefs": ["ev-1"]}],
        "benefits": [],
        "evidence": [
            {
                "evidenceId": "ev-1",
                "documentId": "jd-1",
                "documentSha256": "a" * 64,
                "section": "requirements",
                "text": "Java",
                "charStart": 0,
                "charEnd": 4,
                "page": 1,
                "readingOrder": 0,
                "boundingBox": [1, 1, 2, 2],
            }
        ],
        "parsing": {
            "parserVersion": "deterministic-jd-v1",
            "extractionVersion": "mineru-test",
            "parsedAt": "2026-09-09T00:00:00Z",
            "status": "review_required",
        },
    }


def test_canonical_jd_accepts_api_aliases_and_preserves_evidence() -> None:
    parsed = CanonicalJobDescription.model_validate(_valid_jd())

    assert parsed.requirements[0].minimum_experience_months == 24
    assert parsed.model_dump(by_alias=True)["requirements"][0]["evidenceRefs"] == ["ev-1"]


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda value: value["requirements"][0].update({"evidenceRefs": ["missing"]}), "evidenceRefs"),
        (lambda value: value["requirements"][0].update({"requirementId": ""}), "requirementId"),
        (lambda value: value.update({"unexpected": True}), "unexpected"),
    ],
)
def test_canonical_jd_rejects_unsafe_review_payloads(mutation, message: str) -> None:
    payload = deepcopy(_valid_jd())
    mutation(payload)

    with pytest.raises(ValidationError, match=message):
        CanonicalJobDescription.model_validate(payload)
