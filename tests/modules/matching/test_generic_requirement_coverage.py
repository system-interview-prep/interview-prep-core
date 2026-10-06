from datetime import UTC, datetime

from src.modules.matching.evaluation.requirement_evaluators import (
    evaluate_unresolved_requirement,
    select_evaluator,
)
from src.modules.matching.domain.schemas import UnresolvedRequirement
from src.modules.user_cvs.schemas import CanonicalResume


def _resume(
    *, coverage: str, evidence_coverage: str | None = None, text: str = ""
) -> CanonicalResume:
    return CanonicalResume.model_validate(
        {
            "schemaVersion": "2.1",
            "resumeId": "cv-generic",
            "documentId": "cv-generic",
            "documentSha256": "a" * 64,
            "parsing": {
                "parserVersion": "test",
                "extractionVersion": "test",
                "parsedAt": datetime.now(UTC).isoformat(),
                "status": "review_required",
                "rawTextCoverage": coverage,
                "evidenceIndexCoverage": evidence_coverage
                or ("complete" if coverage == "complete" else "partial"),
            },
            "evidence": (
                [{
                    "evidenceId": "cv-ev-generic",
                    "documentId": "cv-generic",
                    "documentSha256": "a" * 64,
                    "section": "skills",
                    "text": text,
                    "charStart": 0,
                    "charEnd": len(text),
                }]
                if text
                else []
            ),
        }
    ).model_copy(update={"raw_text": text})


def _requirement(label: str) -> UnresolvedRequirement:
    return UnresolvedRequirement(
        requirementId="req-generic",
        type="unresolved",
        kind="experience",
        priority="must_have",
        sourceEvidenceRef="jd-1",
        rawLabel=label,
    )


def test_non_atomic_requirement_always_uses_generic_fallback() -> None:
    requirement = _requirement(
        "Experience building high-performance, high-throughput, or low-latency applications"
    )
    selection = select_evaluator(requirement)
    result = evaluate_unresolved_requirement(
        requirement, _resume(coverage="complete", text="Python web developer.")
    )

    assert selection is not None
    assert selection.name == "generic_requirement"
    assert result.status == "not_met"
    assert result.reason_code == "requirement_not_found"
    assert result.reason_code != "requirement_evaluator_unsupported"
    assert result.evidence_explanation


def test_csharp_absence_in_complete_raw_text_is_not_met() -> None:
    requirement = _requirement("Strong hands-on experience with C# / .NET backend development")
    result = evaluate_unresolved_requirement(
        requirement, _resume(coverage="complete", text="Python API developer.")
    )

    assert result.status == "not_met"
    assert result.reason_code == "requirement_not_found"


def test_partial_coverage_unknown_contains_recovered_evidence() -> None:
    resume = _resume(coverage="partial", text="Performance optimization")
    requirement = _requirement("Strong performance optimization experience")
    result = evaluate_unresolved_requirement(requirement, resume)

    assert result.status == "unknown"
    assert result.reason_code == "generic_requirement_evidence_weak"
    assert result.evidence_refs
    assert result.evidence_explanation
    assert "có nhắc đến" in result.evidence_explanation.lower()


def test_no_candidate_text_without_evidence_is_not_met_even_when_coverage_is_partial() -> None:
    requirement = _requirement("Experience with Kafka")
    result = evaluate_unresolved_requirement(requirement, _resume(coverage="partial"))

    assert result.status == "not_met"
    assert result.reason_code == "requirement_not_found"
    assert not result.evidence_refs
    assert "Không tìm thấy" in (result.evidence_explanation or "")


def test_complete_raw_text_with_partial_evidence_index_without_match_is_not_met() -> None:
    result = evaluate_unresolved_requirement(
        _requirement("Experience with Kafka"),
        _resume(coverage="complete", evidence_coverage="partial"),
    )

    assert result.status == "not_met"
    assert result.reason_code == "requirement_not_found"
    assert not result.evidence_refs


def test_malformed_requirement_without_cv_evidence_is_not_found() -> None:
    requirement = _requirement("and with")
    result = evaluate_unresolved_requirement(requirement, _resume(coverage="complete"))

    assert result.status == "not_met"
    assert result.reason_code == "requirement_not_found"
    assert result.evidence_refs == []
    assert "Không tìm thấy" in (result.evidence_explanation or "")
