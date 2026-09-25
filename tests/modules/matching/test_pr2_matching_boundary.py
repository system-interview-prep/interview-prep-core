from src.modules.job_descriptions.domain.schemas import CanonicalJobDescription
from src.modules.job_descriptions.parsing.deterministic import DeterministicJobDescriptionParser
from src.modules.matching.adapters import job_description_to_matching_job
from src.modules.matching.facade import MatchingFacade
from src.modules.matching.schemas import MatchRequest, SkillRequirement, UnresolvedRequirement
from src.modules.user_cvs.parsing.domain.artifacts import DocumentArtifacts
from src.modules.user_cvs.parsing.domain.source import build_source_document
from tests.modules.matching.test_matching_pipeline import SHA256, StubEmbedder, _evidence, _payload


def _make_source(text: str) -> any:
    lines = text.splitlines()
    return build_source_document(
        DocumentArtifacts(
            markdown=text,
            content_list=[{"type": "text", "text": line, "page_idx": 0} for line in lines],
        ),
        document_id="jd-test",
        document_sha256="a" * 64,
    )


def _base_match_request(parsed_jd: CanonicalJobDescription) -> MatchRequest:
    payload = _payload(add_java=False)
    payload["resume"]["documentSha256"] = SHA256
    adapted_job = job_description_to_matching_job(parsed_jd, job_id="job-boundary-test")
    return MatchRequest(
        schemaVersion="2.1",
        resume=payload["resume"],
        job=adapted_job,
        asyncProcessing=False,
    )


# =========================================================================
# Case A — Atomic decomposition (Python and SQL)
# =========================================================================
def test_boundary_case_a_atomic_decomposition():
    jd_text = (
        "Job Title: Data Engineer\n\n"
        "Requirements:\n"
        "- Strong proficiency in Python and SQL\n"
    )
    parsed = DeterministicJobDescriptionParser().parse(_make_source(jd_text), extraction_version="test")

    # 1. One canonical requirement preserves its atomic concepts and ALL_OF operator.
    skills = [r for r in parsed.requirements if r.kind == "skill"]
    assert len(skills) == 1
    assert skills[0].group_operator == "all_of"
    assert {concept.concept_id for concept in skills[0].atomic_concepts} == {
        "skill-python", "skill-sql"
    }

    # 2. Downstream adapter preserves the group as one evaluable requirement.
    adapted = job_description_to_matching_job(parsed, job_id="job-1")
    assert len(adapted.requirements) == 1
    assert isinstance(adapted.requirements[0], UnresolvedRequirement)
    assert adapted.requirements[0].group_operator == "all_of"
    assert len(adapted.requirements[0].atomic_concepts) == 2

    # 3. MatchingFacade must evaluate both requirements without crashing
    request = _base_match_request(parsed)
    result = MatchingFacade(StubEmbedder()).match(request)
    assert len(result.requirement_results) == 1
    assert len(result.requirement_results[0].concept_results) == 2


# =========================================================================
# Case B — Disjunction (Java or Kotlin)
# =========================================================================
def test_boundary_case_b_disjunction():
    jd_text = (
        "Job Title: Android Developer\n\n"
        "Requirements:\n"
        "- Proficiency in Java or Kotlin\n"
    )
    parsed = DeterministicJobDescriptionParser().parse(_make_source(jd_text), extraction_version="test")

    # 1. Verify parser preserves one ANY_OF requirement with two concepts.
    skills = [r for r in parsed.requirements if r.kind == "skill"]
    assert len(skills) == 1
    assert skills[0].group_operator == "any_of"
    assert len(skills[0].atomic_concepts) == 2

    # 2. Downstream adapter and matcher evaluate cleanly without assuming both mandatory
    adapted = job_description_to_matching_job(parsed, job_id="job-1")
    assert len(adapted.requirements) == 1

    request = _base_match_request(parsed)
    result = MatchingFacade(StubEmbedder()).match(request)
    assert len(result.requirement_results) == 1
    assert result.requirement_results[0].group_operator == "any_of"


# =========================================================================
# Case C — Unknown concept (Strong programming foundation)
# =========================================================================
def test_boundary_case_c_unknown_concept():
    jd_text = (
        "Job Title: Software Engineer\n\n"
        "Requirements:\n"
        "- Strong programming foundation\n"
    )
    parsed = DeterministicJobDescriptionParser().parse(_make_source(jd_text), extraction_version="test")

    # 1. Concept must be None - no hallucinated language
    req = parsed.requirements[0]
    assert req.kind == "skill"
    assert req.concept is None

    # 2. Adapter routes to UnresolvedRequirement
    adapted = job_description_to_matching_job(parsed, job_id="job-1")
    assert isinstance(adapted.requirements[0], UnresolvedRequirement)
    assert adapted.requirements[0].raw_label == "Strong programming foundation"

    # 3. Matcher evaluates fail-closed as unknown / review_required
    request = _base_match_request(parsed)
    result = MatchingFacade(StubEmbedder()).match(request)
    req_res = result.requirement_results[0]
    assert req_res.status == "unknown"
    assert result.eligibility == "review_required"


# =========================================================================
# Case D — Structured threshold (GPA >= 3.2/4.0)
# =========================================================================
def test_boundary_case_d_structured_threshold():
    jd_text = (
        "Job Title: Intern Engineer\n\n"
        "Requirements:\n"
        "- GPA >= 3.2/4.0\n"
    )
    parsed = DeterministicJobDescriptionParser().parse(_make_source(jd_text), extraction_version="test")

    # 1. Verify structured attributes in parsed JD
    req = parsed.requirements[0]
    assert req.kind == "education"
    assert req.operator == "gte"
    assert req.threshold == 3.2
    assert req.scale == 4.0

    # 2. Canonical requirement deserializes cleanly and passes through adapter without crashing
    adapted = job_description_to_matching_job(parsed, job_id="job-1")
    assert isinstance(adapted.requirements[0], UnresolvedRequirement)

    # 3. Matcher handles unresolved education without crashing
    request = _base_match_request(parsed)
    result = MatchingFacade(StubEmbedder()).match(request)
    assert result.eligibility == "review_required"


# =========================================================================
# Case E — Existing JD compatibility (Legacy payload without new fields)
# =========================================================================
def test_boundary_case_e_existing_jd_compatibility():
    # Legacy payload where new fields (group_id, group_operator, operator, threshold, etc.) are absent
    legacy_data = {
        "schemaVersion": "1.0",
        "jobTitle": "Backend Developer",
        "requirements": [
            {
                "requirementId": "req-legacy-1",
                "kind": "skill",
                "priority": "must_have",
                "concept": {
                    "conceptId": "skill-python",
                    "scheme": "internal",
                    "taxonomyVersion": "2026.1",
                    "label": "Python",
                },
                "rawLabel": "Python",
                "evidenceRefs": ["jd-ev-1"],
            }
        ],
        "evidence": [_evidence("jd-ev-1", "doc-1", "Python required")],
        "parsing": {
            "parserVersion": "deterministic-v1",
            "extractionVersion": "legacy-test",
            "parsedAt": "2026-01-01T00:00:00Z",
            "status": "ready",
        },
    }
    parsed = CanonicalJobDescription.model_validate(legacy_data)
    assert parsed.requirements[0].group_id is None
    assert parsed.requirements[0].group_operator is None
    assert parsed.requirements[0].operator is None

    # Downstream adapter and matcher continue to work seamlessly
    adapted = job_description_to_matching_job(parsed, job_id="job-legacy")
    assert len(adapted.requirements) == 1
    assert isinstance(adapted.requirements[0], SkillRequirement)
