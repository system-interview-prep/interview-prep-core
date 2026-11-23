"""A reading can raise a rule verdict and can never lower one.

The policy table in ``judge_evaluators`` is exercised row by row with a
fixed ``ReadingSet``, so no model is involved and every row is a fact about
the code, not about a prompt.
"""

from __future__ import annotations

import pytest

from src.modules.matching.domain.schemas import UnresolvedRequirement
from src.modules.matching.evaluation.judge_evaluators import JUDGE_REASON_CODE
from src.modules.matching.evaluation.requirement_evaluators import (
    evaluate_by_rules,
    evaluate_unresolved_requirement,
)
from src.modules.matching.judge.readings import EntailmentReading, ReadingSet, SelectedSpan
from src.modules.user_cvs.schemas import CanonicalResume

SHA = "c" * 64
CV_LINE = "Chịu trách nhiệm tối ưu truy vấn SQL, giảm thời gian phản hồi từ 800ms xuống 120ms."
GPA_LINE = "GPA: 2.9 / 4.0"


def _resume(*texts: str, complete: bool = True) -> CanonicalResume:
    evidence = [
        {
            "evidenceId": f"cv-{index}",
            "documentId": "resume-judge",
            "documentSha256": SHA,
            "section": "employment",
            "text": text,
            "charStart": index * 1000,
            "charEnd": index * 1000 + len(text),
        }
        for index, text in enumerate(texts, start=1)
    ]
    payload = {
        "schemaVersion": "2.1",
        "resumeId": "resume-judge",
        "documentId": "resume-judge",
        "documentSha256": SHA,
        "evidence": evidence,
        "skills": [],
        "languages": [],
        "employment": [],
        "education": [],
        "projects": [],
        "certifications": [],
        "parsing": {
            "parserVersion": "test",
            "extractionVersion": "test",
            "parsedAt": "2026-01-01T00:00:00Z",
            "status": "ready",
            "rawTextCoverage": "complete" if complete else "partial",
            "evidenceIndexCoverage": "complete" if complete else "partial",
            "canonicalSectionCoverage": {
                "profile": "complete",
                "skills": "complete",
                "employment": "complete",
                "projects": "complete",
                "education": "complete",
                "certifications": "complete",
                "languages": "complete",
            },
            "warnings": [],
        },
    }
    return CanonicalResume.model_validate(payload)


def _requirement(label: str, requirement_id: str = "req-1", **extra) -> UnresolvedRequirement:
    return UnresolvedRequirement.model_validate(
        {
            "requirementId": requirement_id,
            "type": "unresolved",
            "sourceEvidenceRef": "jd-ev-1",
            "kind": "other",
            "priority": "must_have",
            "rawLabel": label,
            **extra,
        }
    )


def _readings(relation: str, *ids: str, requirement_id: str = "req-1") -> ReadingSet:
    return ReadingSet(
        entailments={
            requirement_id: EntailmentReading(
                requirementId=requirement_id,
                relation=relation,
                selected=[SelectedSpan(evidenceId=i, quote="tối ưu truy vấn SQL") for i in ids],
            )
        },
    )


PERFORMANCE = "Strong debugging, problem-solving, and performance optimization skills"


def test_without_readings_the_rule_verdict_is_returned_unchanged() -> None:
    requirement, resume = _requirement(PERFORMANCE), _resume(CV_LINE)

    assert evaluate_unresolved_requirement(requirement, resume) == evaluate_by_rules(requirement, resume)


def test_the_rules_alone_do_not_credit_a_vietnamese_line_for_an_english_requirement() -> None:
    """The premise of every test below: the rules leave this requirement short of met."""

    result = evaluate_by_rules(_requirement(PERFORMANCE), _resume(CV_LINE))

    assert result.status in {"not_met", "unknown"}


def test_a_grounded_satisfies_raises_the_verdict_to_met() -> None:
    requirement, resume = _requirement(PERFORMANCE), _resume(CV_LINE)

    result = evaluate_unresolved_requirement(requirement, resume, _readings("satisfies", "cv-1"))

    assert result.status == "met"
    assert result.reason_code == JUDGE_REASON_CODE
    assert result.evidence_refs == ["cv-1"]
    assert result.evidence_sources == {"cv-1": "document"}
    assert "tối ưu truy vấn SQL" in (result.evidence_explanation or "")


@pytest.mark.parametrize("relation", ["partially", "unrelated", "unsure"])
def test_anything_but_satisfies_leaves_the_rule_verdict_alone(relation: str) -> None:
    requirement, resume = _requirement(PERFORMANCE), _resume(CV_LINE)

    result = evaluate_unresolved_requirement(requirement, resume, _readings(relation, "cv-1"))

    assert result == evaluate_by_rules(requirement, resume)


def test_unrelated_cannot_turn_a_rule_met_into_a_rejection() -> None:
    requirement = _requirement("Experience with Docker")
    resume = _resume("Deployed and maintained services with Docker on AWS.")
    rule = evaluate_by_rules(requirement, resume)
    assert rule.status == "met"

    result = evaluate_unresolved_requirement(requirement, resume, _readings("unrelated", "cv-1"))

    assert result == rule


def test_unsure_on_an_incomplete_cv_stays_unknown_not_not_met() -> None:
    requirement, resume = _requirement(PERFORMANCE), _resume("Unrelated line.", complete=False)

    result = evaluate_unresolved_requirement(requirement, resume, _readings("unsure"))

    assert result.status != "met"
    assert result == evaluate_by_rules(requirement, resume)


def test_a_rule_met_is_not_replaced_by_the_judge() -> None:
    requirement = _requirement("Experience with Docker")
    resume = _resume("Deployed and maintained services with Docker on AWS.")
    rule = evaluate_by_rules(requirement, resume)

    result = evaluate_unresolved_requirement(requirement, resume, _readings("satisfies", "cv-1"))

    assert result == rule


def test_an_entailment_reading_cannot_settle_a_numeric_requirement() -> None:
    """A GPA threshold is a comparison; "satisfies" from the entailment judge is not one.

    Numeric requirements take the numeric path (``test_numeric_judge``), and an
    entailment reading that reached one by mistake changes nothing.
    """

    requirement = _requirement("GPA >= 3.2/4.0", threshold=3.2, scale=4.0, operator="gte")
    resume = _resume(GPA_LINE)
    rule = evaluate_by_rules(requirement, resume)

    result = evaluate_unresolved_requirement(requirement, resume, _readings("satisfies", "cv-1"))

    assert result == rule


def test_a_selected_span_missing_from_the_scoped_resume_is_not_cited() -> None:
    requirement, resume = _requirement(PERFORMANCE), _resume(CV_LINE)

    result = evaluate_unresolved_requirement(requirement, resume, _readings("satisfies", "cv-404"))

    assert result == evaluate_by_rules(requirement, resume)


def test_a_reading_for_another_requirement_is_not_borrowed() -> None:
    requirement, resume = _requirement(PERFORMANCE), _resume(CV_LINE)
    other = _readings("satisfies", "cv-1", requirement_id="req-other")

    result = evaluate_unresolved_requirement(requirement, resume, other)

    assert result == evaluate_by_rules(requirement, resume)
