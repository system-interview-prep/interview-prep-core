from __future__ import annotations

import pytest

from src.modules.job_descriptions.schemas import CanonicalJobDescription
from src.modules.matching.application.facade import MatchingFacade
from src.modules.matching.domain.adapters import job_description_to_matching_job
from src.modules.matching.domain.schemas import MatchRequest, UnresolvedRequirement
from src.modules.matching.evaluation.requirement_evaluators import evaluate_unresolved_requirement
from src.modules.user_cvs.schemas import CanonicalResume

SHA = "c" * 64


class StubEmbedder:
    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        assert len(texts) == 2
        return [[1.0, 0.0], [0.8, 0.6]]


def _evidence(evidence_id: str, document_id: str, text: str, section: str) -> dict:
    return {
        "evidenceId": evidence_id,
        "documentId": document_id,
        "documentSha256": SHA,
        "section": section,
        "text": text,
        "charStart": 0,
        "charEnd": len(text),
        "page": 1,
    }


def _canonical_jd() -> CanonicalJobDescription:
    labels = [
        ("education", "must_have", "Sinh viên năm 4 hoặc mới tốt nghiệp ngành CNTT hoặc liên quan"),
        ("skill", "must_have", "Đam mê nghiên cứu và ứng dụng công nghệ AI"),
        ("education", "must_have", "GPA từ 3.2/4.0 trở lên"),
        ("language", "must_have", "IELTS 6.0 hoặc tương đương"),
        ("skill", "must_have", "Nền tảng lập trình vững chắc; năng khiếu toán học là lợi thế"),
        ("skill", "must_have", "Kiến thức cơ bản về NLP, GenAI và LLM"),
        ("other", "preferred", "Có thành tích trong cuộc thi hoặc nghiên cứu khoa học"),
    ]
    evidence = [
        _evidence(f"jd-ev-{index}", "jd-fixture", label, "requirements")
        for index, (_, _, label) in enumerate(labels, start=1)
    ]
    baseline = [
        {
            "requirementId": f"req-baseline-{index}",
            "kind": kind,
            "priority": priority,
            "rawLabel": label,
            "evidenceRefs": [f"jd-ev-{index}"],
        }
        for index, (kind, priority, label) in enumerate(labels, start=1)
    ]
    llm = [
        {
            "requirementId": f"req-llm-{index:03d}",
            "kind": kind,
            "priority": priority,
            "rawLabel": label,
            "evidenceRefs": [f"jd-ev-{index}"],
        }
        for index, (kind, priority, label) in enumerate(labels, start=1)
    ]
    return CanonicalJobDescription.model_validate(
        {
            "schemaVersion": "1.0",
            "jobTitle": "AI Engineer",
            "requirements": [*baseline, *llm],
            "evidence": evidence,
            "parsing": {
                "parserVersion": "hybrid-jd-v2",
                "extractionVersion": "fixture",
                "parsedAt": "2026-09-21T00:00:00Z",
                "status": "review_required",
            },
        }
    )


def _canonical_resume() -> CanonicalResume:
    texts = [
        ("cv-gpa", "INDUSTRIAL UNIVERSITY OF HO CHI MINH CITY - IUH GPA: 3.3/4.0"),
        ("cv-education", "Information Technology - Software Engineering | Final-year Student"),
        ("cv-competition", "VIN UNIVERSITY - AI20K Build Cohort 3 Competition"),
        ("cv-programming", "Tech: Python, Java, TypeScript, FastAPI and Spring Boot"),
        ("cv-genai", "GenAI Workflow: Built LangGraph and Gemini workflows"),
        ("cv-research", "Scientific Research: An Ensemble NLP System for CV-JD Matching"),
        ("cv-llm", "AI pipeline for NLP inference and OCR/LLM document processing"),
    ]
    return CanonicalResume.model_validate(
        {
            "schemaVersion": "2.1",
            "resumeId": "resume-fixture",
            "documentId": "resume-fixture",
            "documentSha256": SHA,
            "education": [
                {
                    "educationId": "education-iuh",
                    "institution": "Industrial University of Ho Chi Minh City",
                    "degree": "Final-year Student",
                    "fieldOfStudy": "Information Technology - Software Engineering",
                    "studentStatus": "final_year",
                    "gpa": 3.3,
                    "gpaScale": 4.0,
                    "evidenceRefs": ["cv-gpa", "cv-education"],
                }
            ],
            "evidence": [
                _evidence(evidence_id, "resume-fixture", text, "resume")
                for evidence_id, text in texts
            ],
        }
    )


def _resume_with_evidence(evidence_id: str, text: str, section: str) -> CanonicalResume:
    return CanonicalResume.model_validate(
        {
            "schemaVersion": "2.1",
            "resumeId": "resume-evidence-fixture",
            "documentId": "resume-evidence-fixture",
            "documentSha256": SHA,
            "evidence": [_evidence(evidence_id, "resume-evidence-fixture", text, section)],
        }
    )


def _resume_with_certification(name: str, issuer: str, evidence_id: str) -> CanonicalResume:
    text = f"{name} - {issuer}"
    return CanonicalResume.model_validate(
        {
            "schemaVersion": "2.1",
            "resumeId": "resume-certification-fixture",
            "documentId": "resume-certification-fixture",
            "documentSha256": SHA,
            "certifications": [
                {
                    "certificationId": "certification-1",
                    "name": name,
                    "issuer": issuer,
                    "evidenceRefs": [evidence_id],
                }
            ],
            "evidence": [
                _evidence(evidence_id, "resume-certification-fixture", text, "certifications")
            ],
        }
    )


def test_research_project_evidence_matches_but_certification_alone_does_not() -> None:
    requirement = UnresolvedRequirement(
        requirementId="req-research",
        type="unresolved",
        kind="other",
        priority="nice_to_have",
        sourceEvidenceRef="jd-research",
        rawLabel="Có thành tích hoặc giải thưởng trong các cuộc thi hoặc nghiên cứu khoa học",
    )

    research_result = evaluate_unresolved_requirement(
        requirement,
        _resume_with_evidence(
            "cv-research-project",
            "Research Project: Built a movie recommendation system with Graph Neural Networks and PyTorch Geometric.",
            "projects",
        ),
    )
    certification_result = evaluate_unresolved_requirement(
        requirement,
        _resume_with_evidence(
            "cv-certification",
            "Google AI Essentials Specialization; Google Data Analytics Professional Certificate.",
            "certifications",
        ),
    )

    assert research_result.status == "met"
    assert research_result.evidence_refs == ["cv-research-project"]
    assert certification_result.status == "not_met"
    assert certification_result.evidence_refs == []


@pytest.mark.parametrize(
    ("requirement_label", "certification_name", "expected_status"),
    [
        (
            "AWS Certified Solutions Architect certification",
            "AWS Certified Solutions Architect - Associate",
            "met",
        ),
        ("Professional certification", "Google AI Essentials Specialization", "met"),
        (
            "AWS Certified Solutions Architect certification",
            "Google AI Essentials Specialization",
            "not_met",
        ),
    ],
)
def test_professional_certification_requirement_matches_certificate_records(
    requirement_label: str,
    certification_name: str,
    expected_status: str,
) -> None:
    requirement = UnresolvedRequirement(
        requirementId="req-certification",
        type="unresolved",
        kind="other",
        priority="must_have",
        sourceEvidenceRef="jd-certification",
        rawLabel=requirement_label,
    )

    result = evaluate_unresolved_requirement(
        requirement,
        _resume_with_certification(
            certification_name,
            "AWS" if certification_name.startswith("AWS") else "Google",
            "cv-certification",
        ),
    )

    assert result.status == expected_status
    if expected_status == "met":
        assert result.evidence_refs == ["cv-certification"]


def test_fpt_ojt_ai_sample_is_evidence_evaluated_without_duplicate_requirements() -> None:
    job = job_description_to_matching_job(_canonical_jd(), job_id="job-fixture")
    assert len(job.requirements) == 7
    assert all(item.requirement_id.startswith("req-llm-") for item in job.requirements)

    result = MatchingFacade(StubEmbedder()).match(
        MatchRequest(
            schemaVersion="2.1",
            resume=_canonical_resume(),
            job=job,
            matchingPolicy={"bm25ProviderMode": "in_memory"},
            asyncProcessing=False,
        )
    )

    statuses = [item.status for item in result.requirement_results]
    assert statuses.count("met") == 6
    assert statuses.count("unknown") == 0
    assert statuses.count("not_met") == 1
    assert result.requirement_results[0].evidence_refs == ["cv-education"]
    assert result.requirement_results[2].evidence_refs == ["cv-gpa"]
    assert result.requirement_results[3].reason_code == "language_credential_evidence_missing"
    assert result.requirement_results[3].evidence_refs == []
    assert set(result.requirement_results[5].evidence_refs) == {
        "cv-genai",
        "cv-research",
        "cv-llm",
    }
    assert all(
        item.reason_code != "requirement_evaluator_unsupported"
        for item in result.requirement_results
    )

    factors = {item.factor: item for item in result.factor_results}
    assert factors["requirement_coverage"].status == "scored"
    # "Đam mê nghiên cứu và ứng dụng công nghệ AI" is an attitude the posting
    # asks for but a CV cannot evidence, so it is demoted out of the must-have
    # gate.  Its lighter importance weight is what moves coverage off the old
    # all-must-have figure of 0.842519685.
    assert factors["requirement_coverage"].raw_score == pytest.approx(0.8245614035)
    assert factors["skill"].status == "scored"
    assert factors["language"].status == "scored"
    assert factors["experience"].status == "not_applicable"
    assert factors["semantic"].status == "scored"
    assert result.score_provenance.mode == "requirement_aware"
    assert result.score_provenance.supported_requirement_count == 7
    assert result.score_provenance.scored_requirement_count == 7
    assert result.score_provenance.unknown_requirement_count == 0
    assert result.score_provenance.requirement_coverage == pytest.approx(0.8245614035)
    assert sum(result.score_provenance.factor_contributions.values()) == pytest.approx(
        result.diagnostic_score
    )
    # The remaining must-have has no evidence in the CV, so it is now not met.
    assert result.eligibility == "ineligible"
    assert result.suitability_score is None
    assert result.diagnostic_score is not None
    assert result.decision == "assessed"


def test_unresolved_jd_requirement_does_not_get_promoted_by_semantic_similarity() -> None:
    job = job_description_to_matching_job(_canonical_jd(), job_id="job-fixture")
    unsupported = job.requirements[0].model_copy(
        update={"raw_label": "A criterion without an implemented evaluator"}
    )
    job = job.model_copy(update={"requirements": [unsupported]})

    result = MatchingFacade(StubEmbedder()).match(
        MatchRequest(
            schemaVersion="2.1",
            resume=_canonical_resume(),
            job=job,
            matchingPolicy={"bm25ProviderMode": "in_memory"},
            asyncProcessing=False,
        )
    )

    semantic = next(item for item in result.factor_results if item.factor == "semantic")
    assert semantic.status == "scored"
    assert semantic.raw_score is not None
    assert result.suitability_score is None
    # The criterion is explicitly not met; semantic similarity cannot promote it.
    assert result.score_provenance.mode == "requirement_aware"
    assert result.requirement_results[0].status == "not_met"
    assert result.requirement_results[0].reason_code == "requirement_not_evidenced"
