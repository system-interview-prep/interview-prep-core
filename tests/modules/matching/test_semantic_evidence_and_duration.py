from datetime import UTC, datetime
from pathlib import Path

from src.modules.matching.requirement_evaluators import evaluate_unresolved_requirement
from src.modules.matching.schemas import UnresolvedRequirement
from src.modules.user_cvs.schemas import CanonicalResume

FIXTURE = Path("tests/fixtures/matching/mock_cv_semantic_near_match_backend_middle.txt")


def _requirement(
    label: str,
    *,
    priority: str = "must_have",
    kind: str = "experience",
) -> UnresolvedRequirement:
    return UnresolvedRequirement(
        requirementId="req-test",
        type="unresolved",
        kind=kind,
        priority=priority,
        sourceEvidenceRef="jd-1",
        rawLabel=label,
    )


def _resume_from_lines(
    *, employment: list[dict] | None = None, employment_coverage: str | None = None
) -> CanonicalResume:
    raw_text = FIXTURE.read_text(encoding="utf-8")
    lines = [line.strip() for line in raw_text.splitlines() if line.strip() and not line.isupper()]
    evidence = [
        {
            "evidenceId": f"cv-{index}",
            "documentId": "cv-semantic",
            "documentSha256": "a" * 64,
            "section": "raw_text_fallback",
            "text": line,
            "charStart": index,
            "charEnd": index + len(line),
        }
        for index, line in enumerate(lines)
    ]
    parsing = {
        "parserVersion": "test",
        "extractionVersion": "test",
        "parsedAt": datetime.now(UTC).isoformat(),
        "status": "review_required",
        "rawTextCoverage": "complete",
        "evidenceIndexCoverage": "complete",
    }
    if employment_coverage:
        parsing["canonicalSectionCoverage"] = {"employment": employment_coverage}
    return CanonicalResume.model_validate(
        {
            "schemaVersion": "2.1",
            "resumeId": "cv-semantic",
            "documentId": "cv-semantic",
            "documentSha256": "a" * 64,
            "parsing": parsing,
            "evidence": evidence,
            "employment": employment or [],
        }
    )


def test_capability_semantics_are_grounded_and_traceable() -> None:
    result = evaluate_unresolved_requirement(
        _requirement("Experience building high-performance, high-throughput, or low-latency applications"),
        _resume_from_lines(),
    )

    assert result.status == "met"
    assert result.reason_code == "semantic_requirement_evidenced"
    assert result.evidence_refs
    assert result.retrieval_candidates
    assert all(
        candidate.retrieval_method == "semantic_lexical_expansion"
        for candidate in result.retrieval_candidates
    )


def test_semantic_result_metrics_upgrade_query_optimization_evidence() -> None:
    result = evaluate_unresolved_requirement(
        _requirement("Good understanding of database design, indexing, query optimization, and transactions"),
        _resume_from_lines(),
    )

    assert result.status == "met"
    assert result.reason_code == "concept_group_evidenced"
    query_concept = next(
        concept for concept in result.concept_results if "query optimization" in concept.label
    )
    assert query_concept.status == "met"
    assert query_concept.evidence_strength == "demonstrated"


def test_named_technology_is_not_inferred_from_near_synonyms() -> None:
    result = evaluate_unresolved_requirement(
        _requirement("Experience with Kafka, Redis, and MySQL"),
        _resume_from_lines(),
    )

    assert result.status == "not_met"
    assert result.reason_code == "requirement_not_evidenced"
    assert not result.retrieval_candidates


def test_finance_domain_semantics_are_grounded_from_payment_and_ledger_work() -> None:
    result = evaluate_unresolved_requirement(
        _requirement(
            "Experience working on projects related to Finance, Accounting, Fintech, Trading, "
            "or Cryptocurrency",
            priority="nice_to_have",
        ),
        _resume_from_lines(),
    )

    assert result.status == "met"
    assert result.reason_code == "concept_group_evidenced"
    assert result.evidence_refs
    assert result.retrieval_candidates


def test_duration_uses_employment_timeline_and_exposes_evidence() -> None:
    resume = _resume_from_lines(
        employment=[
            {
                "employmentId": "employment-1",
                "jobTitle": "Backend Engineer",
                "startDate": {"value": "2020-01", "precision": "month"},
                "endDate": {"value": "2024-12", "precision": "month"},
                "evidenceRefs": ["cv-0"],
            }
        ]
    )

    result = evaluate_unresolved_requirement(
        _requirement("Minimum 3 years of professional Backend Development experience"),
        resume,
    )

    assert result.status == "met"
    assert result.reason_code == "experience_duration_satisfied"
    assert result.evidence_refs == ["cv-0"]


def test_bachelor_degree_satisfies_college_or_higher_requirement() -> None:
    resume = CanonicalResume.model_validate(
        {
            "schemaVersion": "2.1",
            "resumeId": "cv-degree",
            "documentId": "cv-degree",
            "documentSha256": "b" * 64,
            "parsing": {
                "parserVersion": "test",
                "extractionVersion": "test",
                "parsedAt": datetime.now(UTC).isoformat(),
                "status": "review_required",
                "rawTextCoverage": "complete",
                "evidenceIndexCoverage": "complete",
            },
            "education": [
                {
                    "educationId": "education-1",
                    "institution": "Ho Chi Minh City University of Technology",
                    "degree": "B.Sc.",
                    "fieldOfStudy": "Computer Science",
                    "evidenceRefs": ["degree-ev"],
                }
            ],
            "evidence": [
                {
                    "evidenceId": "degree-ev",
                    "documentId": "cv-degree",
                    "documentSha256": "b" * 64,
                    "section": "education",
                    "text": "B.Sc. in Computer Science — Ho Chi Minh City University of Technology | 2020",
                    "charStart": 0,
                    "charEnd": len(
                        "B.Sc. in Computer Science — Ho Chi Minh City University of Technology | 2020"
                    ),
                }
            ],
        }
    )
    result = evaluate_unresolved_requirement(
        _requirement("Cao Đẳng trở lên", kind="education"),
        resume,
    )

    assert result.status == "met"
    assert result.reason_code == "education_degree_evidenced"
    assert result.evidence_refs == ["degree-ev"]


def test_duration_union_does_not_double_count_overlapping_employment() -> None:
    resume = _resume_from_lines(
        employment=[
            {
                "employmentId": "employment-1",
                "jobTitle": "Backend Engineer",
                "startDate": {"value": "2020-01", "precision": "month"},
                "endDate": {"value": "2022-12", "precision": "month"},
                "evidenceRefs": ["cv-0"],
            },
            {
                "employmentId": "employment-2",
                "jobTitle": "Platform Engineer",
                "startDate": {"value": "2022-01", "precision": "month"},
                "endDate": {"value": "2023-12", "precision": "month"},
                "evidenceRefs": ["cv-1"],
            },
        ]
    )

    result = evaluate_unresolved_requirement(
        _requirement("Minimum 5 years of professional Backend Development experience"),
        resume,
    )

    assert result.status == "not_met"
    assert result.reason_code == "experience_duration_below_minimum"


def test_duration_without_employment_timeline_remains_unknown_with_reason() -> None:
    result = evaluate_unresolved_requirement(
        _requirement("Minimum 3 years of professional Backend Development experience"),
        _resume_from_lines(),
    )

    assert result.status == "unknown"
    assert result.reason_code == "experience_duration_evidence_missing"
    assert "timeline" in (result.evidence_explanation or "").lower()


def test_complete_empty_employment_is_not_met_for_duration_requirement() -> None:
    result = evaluate_unresolved_requirement(
        _requirement("Minimum 3 years of professional Backend Development experience"),
        _resume_from_lines(employment_coverage="complete_empty"),
    )

    assert result.status == "not_met"
    assert result.reason_code == "experience_duration_evidence_missing"
