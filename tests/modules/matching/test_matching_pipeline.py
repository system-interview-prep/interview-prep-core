import pytest
from pydantic import ValidationError

from src.modules.matching.facade import MatchingFacade
from src.modules.matching.schemas import MatchRequest

SHA256 = "a" * 64


class StubEmbedder:
    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        assert len(texts) == 2
        return [[1.0, 0.0], [1.0, 0.0]]


class CapturingEmbedder(StubEmbedder):
    def __init__(self) -> None:
        self.texts: list[str] = []

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        self.texts = texts
        return super().embed_texts(texts)


def _evidence(evidence_id: str, document_id: str, text: str) -> dict:
    return {
        "evidenceId": evidence_id,
        "documentId": document_id,
        "documentSha256": SHA256,
        "section": "skills",
        "text": text,
        "charStart": 0,
        "charEnd": len(text),
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
            "evidence": [_evidence("cv-ev-java", "cv-doc-1", "Java experience")],
        },
        "job": {
            "schemaVersion": "2.1",
            "jobId": "job-1",
            "documentId": "job-doc-1",
            "documentSha256": SHA256,
            "evidence": [_evidence("jd-ev-java", "job-doc-1", "Requires Java experience")],
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
        "matchingPolicy": {"policyVersion": "balanced-v1"},
    }


def test_unified_pipeline_marks_evidenced_must_have_as_eligible() -> None:
    result = MatchingFacade(StubEmbedder()).match(MatchRequest.model_validate(_payload()))
    assert result.eligibility == "eligible"
    assert result.decision == "assessed"
    assert result.requirement_results[0].status == "met"
    assert result.requirement_results[0].evidence_refs == ["cv-ev-java"]
    assert sum(item.effective_weight for item in result.factor_results) == pytest.approx(1.0)
    assert (
        next(item for item in result.factor_results if item.factor == "experience").status == "not_applicable"
    )


def test_semantic_score_cannot_compensate_for_failed_must_have() -> None:
    result = MatchingFacade(StubEmbedder()).match(MatchRequest.model_validate(_payload(java_months=12)))
    assert result.eligibility == "ineligible"
    assert result.fit_band == "not_eligible"
    assert result.requirement_results[0].status == "not_met"
    assert result.suitability_score is None
    assert result.diagnostic_score is not None
    assert result.failed_must_have_requirements == ["req-java"]


def test_missing_must_have_requires_manual_review() -> None:
    result = MatchingFacade(StubEmbedder()).match(MatchRequest.model_validate(_payload(add_java=False)))
    assert result.eligibility == "review_required"
    assert result.decision == "abstained"
    assert result.requirement_results[0].status == "unknown"
    assert result.suitability_score is None
    assert result.diagnostic_score is not None
    assert result.failed_must_have_requirements == []


def test_named_skill_context_keeps_unknown_but_exposes_related_evidence() -> None:
    payload = _payload()
    payload["resume"]["evidence"] = [
        _evidence("cv-ev-java", "cv-doc-1", "Packaged services as OCI images on Unix-like hosts"),
    ]
    payload["resume"]["skills"] = []
    payload["job"]["requirements"] = [
        {
            "requirementId": "req-docker",
            "type": "skill",
            "priority": "nice_to_have",
            "sourceEvidenceRef": "jd-ev-java",
            "skill": {
                "conceptId": "skill-docker",
                "scheme": "internal",
                "taxonomyVersion": "2026.1",
                "label": "Docker",
            },
            "rawLabel": "Experience with Docker and Linux environments",
            "operator": "required",
        }
    ]

    result = MatchingFacade(StubEmbedder()).match(MatchRequest.model_validate(payload))

    assert result.requirement_results[0].status == "unknown"
    assert result.requirement_results[0].reason_code == "skill_semantic_context_needs_confirmation"
    assert result.requirement_results[0].evidence_refs == ["cv-ev-java"]


def test_unknown_requirement_is_excluded_from_coverage_score_but_exposed_in_provenance() -> None:
    payload = _payload()
    payload["job"]["requirements"].append(
        {
            "requirementId": "req-language",
            "type": "language",
            "priority": "must_have",
            "sourceEvidenceRef": "jd-ev-java",
            "languageCode": "en",
            "operator": "required",
        }
    )
    result = MatchingFacade(StubEmbedder()).match(MatchRequest.model_validate(payload))

    coverage = next(item for item in result.factor_results if item.factor == "requirement_coverage")
    assert coverage.raw_score == 1.0
    assert coverage.reliability == pytest.approx(0.5)
    assert result.score_provenance.unknown_requirement_count == 1


def test_grounded_coverage_contribution_exceeds_semantic_contribution() -> None:
    result = MatchingFacade(StubEmbedder()).match(MatchRequest.model_validate(_payload()))

    contributions = result.score_provenance.factor_contributions
    assert contributions["requirement_coverage"] > contributions["semantic"]
    assert sum(contributions.values()) == pytest.approx(result.suitability_score, abs=1e-6)


def test_schema_rejects_invalid_requirement_constraint() -> None:
    payload = _payload()
    payload["job"]["requirements"][0].pop("minimumExperienceMonths")
    with pytest.raises(ValidationError, match="minimumExperienceMonths"):
        MatchRequest.model_validate(payload)


def test_proficiency_requirement_uses_ordered_level_and_evidence() -> None:
    payload = _payload()
    claim = payload["resume"]["skills"][0]
    claim.pop("experienceMonths")
    claim["proficiencyLevel"] = "beginner"
    requirement = payload["job"]["requirements"][0]
    requirement["operator"] = "proficiency_gte"
    requirement.pop("minimumExperienceMonths")
    requirement["minimumProficiencyLevel"] = "intermediate"

    result = MatchingFacade(StubEmbedder()).match(MatchRequest.model_validate(payload))

    assert result.requirement_results[0].status == "not_met"
    assert result.requirement_results[0].reason_code == "skill_level_below_minimum"
    assert result.requirement_results[0].evidence_refs == ["cv-ev-java"]


def test_title_alone_does_not_make_experience_factor_applicable() -> None:
    payload = _payload()
    payload["resume"]["employment"] = [
        {
            "employmentId": "employment-1",
            "jobTitle": "Backend Developer",
            "isCurrent": True,
            "evidenceRefs": ["cv-ev-java"],
        }
    ]
    payload["job"]["jobTitle"] = "Backend Developer"

    result = MatchingFacade(StubEmbedder()).match(MatchRequest.model_validate(payload))

    experience = next(item for item in result.factor_results if item.factor == "experience")
    assert experience.status == "not_applicable"
    assert experience.raw_score is None
    assert experience.evidence_refs == []


def test_semantic_factor_prefers_responsibility_project_and_achievement_context() -> None:
    payload = _payload()
    payload["resume"]["employment"] = [
        {
            "employmentId": "employment-1",
            "jobTitle": "Backend Developer",
            "responsibilities": ["Designed payment APIs"],
            "achievements": [
                {
                    "text": "Reduced API latency by 35%",
                    "evidenceRefs": ["cv-ev-java"],
                }
            ],
            "evidenceRefs": ["cv-ev-java"],
        }
    ]
    payload["job"]["responsibilities"] = [
        {"text": "Build reliable payment APIs", "evidenceRefs": ["jd-ev-java"]}
    ]
    embedder = CapturingEmbedder()

    MatchingFacade(embedder).match(MatchRequest.model_validate(payload))

    assert "Designed payment APIs" in embedder.texts[0]
    assert "Reduced API latency by 35%" in embedder.texts[0]
    assert "Java" in embedder.texts[1]
    assert "Build reliable payment APIs" in embedder.texts[1]


def test_matching_facade_builds_the_default_embedder_once_per_batch(monkeypatch) -> None:
    created = []

    def build_embedder():
        embedder = StubEmbedder()
        created.append(embedder)
        return embedder

    monkeypatch.setattr("src.modules.matching.facade.build_embedding_adapter_from_env", build_embedder)
    facade = MatchingFacade()
    request = MatchRequest.model_validate(_payload())

    facade.match(request)
    facade.match(request)

    assert len(created) == 1


def test_work_mode_and_location_are_compatibility_not_suitability() -> None:
    payload = _payload()
    payload["matchingPolicy"]["bm25ProviderMode"] = "in_memory"
    payload["job"].update({"workMode": "on_site", "location": "Hà Nội"})
    payload["candidatePreferences"] = {
        "acceptedWorkModes": ["remote"],
        "acceptedLocations": ["Hà Nội"],
        "willingToRelocate": False,
    }
    incompatible = MatchingFacade(StubEmbedder()).match(MatchRequest.model_validate(payload))

    payload["candidatePreferences"]["acceptedWorkModes"] = ["on_site"]
    compatible = MatchingFacade(StubEmbedder()).match(MatchRequest.model_validate(payload))

    assert incompatible.compatibility_status == "incompatible"
    assert compatible.compatibility_status == "compatible"
    assert incompatible.suitability_score == pytest.approx(compatible.suitability_score)


def test_missing_candidate_preference_returns_unknown_compatibility() -> None:
    payload = _payload()
    payload["job"].update({"workMode": "hybrid", "location": "Da Nang"})

    result = MatchingFacade(StubEmbedder()).match(MatchRequest.model_validate(payload))

    assert result.compatibility_status == "unknown"
    assert {item.status for item in result.compatibility_results} == {"unknown"}


def test_matching_contract_rejects_identity_pii() -> None:
    payload = _payload()
    payload["resume"]["identity"] = {"dateOfBirth": {"value": "1990", "charStart": 0, "charEnd": 4}}

    with pytest.raises(ValidationError):
        MatchRequest.model_validate(payload)


def test_grounded_job_fields_require_resolvable_evidence() -> None:
    payload = _payload()
    payload["job"]["responsibilities"] = [
        {"text": "Build APIs", "evidenceRefs": ["missing-evidence"]}
    ]

    with pytest.raises(ValidationError, match="job evidenceRefs"):
        MatchRequest.model_validate(payload)
