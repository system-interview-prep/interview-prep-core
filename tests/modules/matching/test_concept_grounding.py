import pytest
from pydantic import ValidationError

from src.modules.matching.requirement_evaluators import evaluate_unresolved_requirement
from src.modules.matching.schemas import RequirementResult, UnresolvedRequirement
from src.modules.user_cvs.schemas import CanonicalResume

SHA = "d" * 64


def _resume(*texts: str) -> CanonicalResume:
    return CanonicalResume.model_validate(
        {
            "schemaVersion": "2.1",
            "resumeId": "resume-grounding",
            "documentId": "resume-grounding",
            "documentSha256": SHA,
            "evidence": [
                {
                    "evidenceId": f"cv-{index}",
                    "documentId": "resume-grounding",
                    "documentSha256": SHA,
                    "section": "projects",
                    "text": text,
                    "charStart": 0,
                    "charEnd": len(text),
                }
                for index, text in enumerate(texts, start=1)
            ],
        }
    )


def _requirement(
    label: str,
    *concepts: tuple[str, str],
    operator: str | None = None,
) -> UnresolvedRequirement:
    return UnresolvedRequirement(
        requirementId="requirement-grounding",
        type="unresolved",
        kind="skill",
        priority="must_have",
        sourceEvidenceRef="jd-evidence",
        rawLabel=label,
        atomicConcepts=[
            {
                "conceptId": concept_id,
                "scheme": "test-taxonomy",
                "taxonomyVersion": "1",
                "label": concept_label,
            }
            for concept_id, concept_label in concepts
        ],
        groupOperator=operator,
    )


def test_all_of_concepts_are_grounded_independently() -> None:
    result = evaluate_unresolved_requirement(
        _requirement(
            "Basic knowledge of NLP, GenAI and LLM",
            ("skill-nlp", "NLP"),
            ("skill-genai", "GenAI"),
            ("skill-llm", "LLM"),
            operator="all_of",
        ),
        _resume(
            "NLP Research: Researched an ensemble scorer combining BM25, TF-IDF and NER.",
            "GenAI Workflow: Built LangGraph and Gemini workflows.",
            "AI Pipeline: OCR/LLM document processing.",
        ),
    )

    assert result.group_operator == "all_of"
    assert result.status == "met"
    assert [item.label for item in result.concept_results] == ["NLP", "GenAI", "LLM"]
    assert [item.evidence_refs for item in result.concept_results] == [["cv-1"], ["cv-2"], ["cv-3"]]
    assert result.evidence_refs == ["cv-1", "cv-2", "cv-3"]
    assert result.concept_results[0].evidence_strength == "demonstrated"
    assert result.concept_results[1].evidence_strength == "applied"


def test_all_of_missing_one_concept_is_unknown() -> None:
    result = evaluate_unresolved_requirement(
        _requirement(
            "Basic knowledge of NLP, GenAI and LLM",
            ("skill-nlp", "NLP"),
            ("skill-genai", "GenAI"),
            ("skill-llm", "LLM"),
            operator="all_of",
        ),
        _resume("Built NLP pipeline.", "Built GenAI workflow."),
    )

    assert result.status == "unknown"
    assert [item.status for item in result.concept_results] == ["met", "met", "unknown"]
    assert result.concept_results[2].evidence_refs == []


def test_any_of_one_supported_concept_is_met() -> None:
    result = evaluate_unresolved_requirement(
        _requirement(
            "Python or Java",
            ("skill-python", "Python"),
            ("skill-java", "Java"),
            operator="any_of",
        ),
        _resume("Built a Python service for document processing."),
    )

    assert result.group_operator == "any_of"
    assert result.status == "met"
    assert result.concept_results[0].status == "met"
    assert result.concept_results[1].status == "unknown"


def test_mention_is_not_sufficient_for_production_experience() -> None:
    result = evaluate_unresolved_requirement(
        _requirement("Production experience implementing NLP", ("skill-nlp", "NLP")),
        _resume("NLP"),
    )

    assert result.status == "unknown"
    assert result.concept_results[0].evidence_strength == "mention"
    assert result.concept_results[0].reason_code == "concept_evidence_too_weak"


def test_concept_keeps_only_two_strongest_deduplicated_evidence_refs() -> None:
    result = evaluate_unresolved_requirement(
        _requirement("Basic knowledge of NLP", ("skill-nlp", "NLP")),
        _resume(
            "NLP",
            "Skills: NLP",
            "Built an NLP pipeline.",
            "Researched NLP benchmark evaluation.",
        ),
    )

    concept = result.concept_results[0]
    assert concept.evidence_refs == ["cv-4", "cv-3"]
    assert result.evidence_refs == concept.evidence_refs


def test_no_evidence_is_unknown_and_similarity_cannot_promote_it() -> None:
    result = evaluate_unresolved_requirement(
        _requirement("Experience with Rust", ("skill-rust", "Rust")),
        _resume("Built a high-performance distributed systems service."),
    )

    assert result.status == "unknown"
    assert result.concept_results[0].status == "unknown"
    assert result.concept_results[0].evidence_refs == []


def test_taxonomy_concepts_are_generic_not_fixture_specific() -> None:
    result = evaluate_unresolved_requirement(
        _requirement(
            "Ruby or Elixir",
            ("skill-ruby", "Ruby"),
            ("skill-elixir", "Elixir"),
            operator="any_of",
        ),
        _resume("Built production services with Elixir."),
    )

    assert result.status == "met"
    assert [concept.label for concept in result.concept_results] == ["Ruby", "Elixir"]
    assert [concept.status for concept in result.concept_results] == ["unknown", "met"]


def test_same_evidence_found_via_claim_and_text_is_deduplicated() -> None:
    resume = CanonicalResume.model_validate(
        {
            "schemaVersion": "2.1",
            "resumeId": "resume-dedupe",
            "documentId": "resume-dedupe",
            "documentSha256": SHA,
            "skills": [{
                "claimId": "claim-python",
                "concept": {
                    "conceptId": "skill-python",
                    "scheme": "test-taxonomy",
                    "taxonomyVersion": "1",
                    "label": "Python",
                },
                "rawLabel": "Python",
                "evidenceRefs": ["cv-python"],
            }],
            "evidence": [{
                "evidenceId": "cv-python",
                "documentId": "resume-dedupe",
                "documentSha256": SHA,
                "section": "projects",
                "text": "Built a Python API.",
                "charStart": 10,
                "charEnd": 29,
                "page": 2,
            }],
        }
    )
    result = evaluate_unresolved_requirement(
        _requirement("Python", ("skill-python", "Python")), resume
    )

    assert result.concept_results[0].evidence_refs == ["cv-python"]
    evidence = {item.evidence_id: item for item in resume.evidence}["cv-python"]
    assert (evidence.document_id, evidence.page, evidence.section) == (
        "resume-dedupe", 2, "projects"
    )
    assert evidence.text == "Built a Python API."


def test_requirement_result_rejects_non_union_evidence_and_partial_status() -> None:
    with pytest.raises(ValidationError, match="ordered union"):
        RequirementResult.model_validate({
            "requirementId": "req-1",
            "status": "met",
            "score": 1,
            "confidence": 0.8,
            "evidenceRefs": ["extra"],
            "reasonCode": "concept_group_evidenced",
            "conceptResults": [{
                "conceptId": "skill-python",
                "label": "Python",
                "status": "met",
                "confidence": 0.8,
                "evidenceRefs": ["cv-python"],
                "evidenceStrength": "applied",
                "reasonCode": "concept_evidence_sufficient",
            }],
            "groupOperator": "atomic",
        })
    with pytest.raises(ValidationError):
        RequirementResult.model_validate({
            "requirementId": "req-1",
            "status": "partial",
            "confidence": 0.5,
            "reasonCode": "unsupported",
        })
