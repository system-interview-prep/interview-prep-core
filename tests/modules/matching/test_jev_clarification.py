from src.modules.matching.ambiguity import (
    ClarificationRequest,
    NoopAmbiguityAnalyzer,
    build_clarification_requests,
)
from src.modules.matching.facade import MatchingFacade
from src.modules.matching.schemas import MatchRequest

SHA256 = "a" * 64


class StubEmbedder:
    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0] for _ in texts]


class CapturingAnalyzer:
    def __init__(self) -> None:
        self.requirement_ids: list[str] = []

    def analyze(self, requirement, resume):
        del resume
        self.requirement_ids.append(requirement.requirement_id)
        return ClarificationRequest(
            requirementId=requirement.requirement_id,
            missingDimension="scale",
            confidence=0.91,
            evidenceRefs=["cv-ev-backend"],
            promptKey="matching.clarification.scale",
        )


def _evidence(evidence_id: str, document_id: str, text: str) -> dict:
    return {
        "evidenceId": evidence_id,
        "documentId": document_id,
        "documentSha256": SHA256,
        "section": "experience",
        "text": text,
        "charStart": 0,
        "charEnd": len(text),
        "page": 1,
    }


def _unknown_payload() -> dict:
    return {
        "schemaVersion": "2.1",
        "resume": {
            "schemaVersion": "2.1",
            "resumeId": "cv-1",
            "documentId": "cv-doc-1",
            "documentSha256": SHA256,
            "evidence": [
                _evidence(
                    "cv-ev-backend",
                    "cv-doc-1",
                    "Developed Spring Boot backend services.",
                )
            ],
        },
        "job": {
            "schemaVersion": "2.1",
            "jobId": "job-1",
            "documentId": "job-doc-1",
            "documentSha256": SHA256,
            "evidence": [
                _evidence(
                    "jd-ev-backend",
                    "job-doc-1",
                    "Strong experience designing scalable backend systems.",
                )
            ],
            "requirements": [
                {
                    "requirementId": "req-backend-scale",
                    "type": "unresolved",
                    "kind": "experience",
                    "priority": "must_have",
                    "sourceEvidenceRef": "jd-ev-backend",
                    "rawLabel": "Strong experience designing scalable backend systems",
                }
            ],
        },
        "matchingPolicy": {"policyVersion": "balanced-v1"},
        "asyncProcessing": False,
    }


def _met_payload() -> dict:
    payload = _unknown_payload()
    payload["resume"]["skills"] = [
        {
            "claimId": "claim-java",
            "concept": {
                "conceptId": "skill-java",
                "scheme": "internal",
                "taxonomyVersion": "2026.1",
                "label": "Java",
            },
            "rawLabel": "Java",
            "experienceMonths": 48,
            "evidenceRefs": ["cv-ev-backend"],
        }
    ]
    payload["job"]["requirements"] = [
        {
            "requirementId": "req-java",
            "type": "skill",
            "priority": "must_have",
            "sourceEvidenceRef": "jd-ev-backend",
            "skill": {
                "conceptId": "skill-java",
                "scheme": "internal",
                "taxonomyVersion": "2026.1",
                "label": "Java",
            },
            "operator": "gte",
            "minimumExperienceMonths": 36,
        }
    ]
    return payload


def test_unknown_requirement_can_produce_candidate_clarification_without_mutating_match() -> None:
    request = MatchRequest.model_validate(_unknown_payload())
    result = MatchingFacade(StubEmbedder()).match(request)
    before = result.model_dump(mode="json", by_alias=True)
    analyzer = CapturingAnalyzer()

    clarifications = build_clarification_requests(request, result, analyzer)

    assert result.requirement_results[0].status == "unknown"
    assert analyzer.requirement_ids == ["req-backend-scale"]
    assert len(clarifications) == 1
    assert clarifications[0].missing_dimension == "scale"
    assert clarifications[0].confidence == 0.91
    assert result.model_dump(mode="json", by_alias=True) == before


def test_analyzer_is_not_called_for_already_resolved_requirement() -> None:
    request = MatchRequest.model_validate(_met_payload())
    result = MatchingFacade(StubEmbedder()).match(request)
    analyzer = CapturingAnalyzer()

    clarifications = build_clarification_requests(request, result, analyzer)

    assert result.requirement_results[0].status == "met"
    assert analyzer.requirement_ids == []
    assert clarifications == []


def test_noop_analyzer_preserves_existing_unknown_behavior() -> None:
    request = MatchRequest.model_validate(_unknown_payload())
    result = MatchingFacade(StubEmbedder()).match(request)
    before = result.model_dump(mode="json", by_alias=True)

    clarifications = build_clarification_requests(
        request,
        result,
        NoopAmbiguityAnalyzer(),
    )

    assert result.requirement_results[0].status == "unknown"
    assert clarifications == []
    assert result.model_dump(mode="json", by_alias=True) == before
