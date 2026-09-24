from __future__ import annotations

import pytest

from src.modules.job_descriptions.schemas import CanonicalJobDescription
from src.modules.matching.adapters import job_description_to_matching_job
from src.modules.matching.facade import MatchingFacade
from src.modules.matching.schemas import MatchRequest
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
    assert statuses.count("unknown") == 1
    assert statuses.count("not_met") == 0
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
    assert factors["requirement_coverage"].raw_score == 1.0
    assert factors["skill"].status == "scored"
    assert factors["language"].status == "unknown"
    assert factors["experience"].status == "not_applicable"
    assert factors["semantic"].status == "scored"
    assert result.score_provenance.mode == "requirement_aware"
    assert result.score_provenance.supported_requirement_count == 7
    assert result.score_provenance.scored_requirement_count == 6
    assert result.score_provenance.unknown_requirement_count == 1
    assert result.score_provenance.requirement_coverage == 1.0
    assert sum(result.score_provenance.factor_contributions.values()) == pytest.approx(
        result.suitability_score
    )
    assert result.eligibility == "eligible"
    assert result.decision == "assessed"


def test_semantic_only_similarity_is_exposed_as_a_low_evidence_estimate() -> None:
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
    assert result.suitability_score == semantic.raw_score
    assert result.score_provenance.mode == "semantic_only_estimated"
    assert "semantic_only_estimate" in result.warnings
