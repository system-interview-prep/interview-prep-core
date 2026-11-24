"""The numeric judge extracts a number; the rules compare it.

Every reading must point at a handed-in span, quote it verbatim and contain
the digits it claims.  Then the rule evaluator is re-run over the CV with the
span restated, so the verdict - met or not_met - is the rules' own.
"""

from __future__ import annotations

import json

import pytest

from src.modules.matching.domain.schemas import UnresolvedRequirement
from src.modules.matching.evaluation.requirement_evaluators import (
    evaluate_by_rules,
    evaluate_unresolved_requirement,
)
from src.modules.matching.judge.numeric_judge import NumericJudge, number_in_quote
from src.modules.matching.judge.readings import NumericReading, ReadingSet
from src.modules.user_cvs.schemas import CanonicalResume, EvidenceSpan

SHA = "c" * 64
TORN = "Bachelor’s Degree in Information Technology (Major in Data Science)GP A: 3.25/4.00"


def _resume(*texts: str, section: str = "education") -> CanonicalResume:
    evidence = [
        {
            "evidenceId": f"cv-{index}",
            "documentId": "resume-numeric",
            "documentSha256": SHA,
            "section": section,
            "text": text,
            "charStart": index * 1000,
            "charEnd": index * 1000 + len(text),
        }
        for index, text in enumerate(texts, start=1)
    ]
    return CanonicalResume.model_validate(
        {
            "schemaVersion": "2.1",
            "resumeId": "resume-numeric",
            "documentId": "resume-numeric",
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
                "rawTextCoverage": "complete",
                "evidenceIndexCoverage": "complete",
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
    )


def _requirement(label: str, kind: str = "education", **extra) -> UnresolvedRequirement:
    return UnresolvedRequirement.model_validate(
        {
            "requirementId": "req-1",
            "type": "unresolved",
            "sourceEvidenceRef": "jd-ev-1",
            "kind": kind,
            "priority": "must_have",
            "rawLabel": label,
            **extra,
        }
    )


def _reading(**fields) -> NumericReading:
    base = {
        "requirementId": "req-1",
        "kind": "gpa",
        "value": 3.25,
        "scale": 4.0,
        "evidenceId": "cv-1",
        "quote": "GP A: 3.25/4.00",
    }
    return NumericReading.model_validate({**base, **fields})


def _readings(reading: NumericReading | None) -> ReadingSet:
    return ReadingSet(numerics={} if reading is None else {reading.requirement_id: reading})


class ScriptedClient:
    def __init__(self, answer: dict | str | Exception) -> None:
        self.answer = answer

    def complete(self, *, instructions: str, input_text: str, output_schema: dict) -> str:
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer if isinstance(self.answer, str) else json.dumps(self.answer)


def _answer(**fields) -> dict:
    base = {
        "found": True,
        "kind": "gpa",
        "value": 3.25,
        "scale": 4.0,
        "credential": None,
        "evidence_id": "cv-1",
        "quote": "GP A: 3.25/4.00",
    }
    return {**base, **fields}


# --- number_in_quote --------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "quote"),
    [
        (3.25, "GP A: 3.25/4.00"),
        (4.0, "GP A: 3.25/4.00"),
        (3.25, "GPA 3,25 / 4"),
        (6.5, "IELTS 6.5 (2025)"),
        (900, "TOEIC: 900"),
        (3.0, "GPA: 3.0/4.0"),
    ],
)
def test_the_number_is_found_however_it_was_spaced_or_marked(value: float, quote: str) -> None:
    assert number_in_quote(value, quote)


@pytest.mark.parametrize(
    ("value", "quote"),
    [
        (3.2, "GP A: 3.25/4.00"),  # a prefix of another number
        (25, "GP A: 3.25/4.00"),  # a suffix of another number
        (3.5, "GPA 3.25/4"),  # not written at all
        (7.0, "IELTS 6.5"),
    ],
)
def test_a_number_the_quote_does_not_write_is_not_found(value: float, quote: str) -> None:
    assert not number_in_quote(value, quote)


# --- the judge's checks -------------------------------------------------------------


def _candidates(*texts: str) -> list[EvidenceSpan]:
    return list(_resume(*texts).evidence)


def _read(answer) -> tuple:
    outcome = NumericJudge(ScriptedClient(answer)).read(
        requirement_id="req-1", requirement_text="GPA từ 3.2/4.0 trở lên", candidates=_candidates(TORN)
    )
    return outcome.reading, outcome.dropped, outcome.fallback


def test_a_grounded_extraction_becomes_a_reading_restated_for_the_rules() -> None:
    reading, dropped, fallback = _read(_answer())

    assert dropped is None and fallback is None
    assert reading is not None
    assert reading.restated() == "GPA 3.25/4"
    assert reading.evidence_id == "cv-1"


@pytest.mark.parametrize(
    ("answer", "reason"),
    [
        (_answer(found=False, value=None, scale=None, quote=""), "not_found"),
        (_answer(evidence_id="cv-9"), "unknown_id"),
        (_answer(quote="GPA: 3.25/4.00"), "quote_not_in_span"),  # "fixed" quote is not verbatim
        (_answer(value=3.52), "value_not_in_quote"),
        (_answer(scale=10), "scale_not_in_quote"),
        (_answer(kind="credential_score", credential=None), "no_credential"),
        (_answer(kind="credential_score", credential="ielts"), "credential_not_in_quote"),
    ],
)
def test_an_extraction_that_fails_a_check_yields_no_reading(answer: dict, reason: str) -> None:
    reading, dropped, _ = _read(answer)

    assert reading is None
    assert dropped == reason


def test_transport_and_schema_failures_are_fallbacks() -> None:
    assert _read(ConnectionError("down"))[2] == "transport"
    assert _read("not json")[2] == "schema"
    assert _read(json.dumps({"found": True}))[2] == "schema"


# --- the policy: rules compare, both ways -------------------------------------------


def test_torn_gpa_text_is_read_and_meets_the_floor() -> None:
    """The case this exists for: the regex misses "GP A", the judge does not, the rules compare."""
    requirement = _requirement("GPA từ 3.2/4.0 trở lên", threshold=3.2, scale=4.0, operator="gte")
    resume = _resume(TORN)
    assert evaluate_by_rules(requirement, resume).reason_code == "gpa_not_evidenced"

    result = evaluate_unresolved_requirement(requirement, resume, _readings(_reading()))

    assert result.status == "met"
    assert result.reason_code == "gpa_threshold_met"
    assert result.evidence_refs == ["cv-1"]
    assert result.confidence == 0.85
    assert "GP A: 3.25/4.00" in (result.evidence_explanation or "")


def test_a_verified_number_short_of_the_floor_is_not_met() -> None:
    """Allowed on purpose: the number was checked against the CV, the comparison is the rules'."""
    requirement = _requirement("GPA từ 3.2/4.0 trở lên", threshold=3.2, scale=4.0, operator="gte")
    resume = _resume("Information Technology (Data Science)GP A: 2.90/4.00")
    reading = _reading(value=2.9, quote="GP A: 2.90/4.00")

    result = evaluate_unresolved_requirement(requirement, resume, _readings(reading))

    assert result.status == "not_met"
    assert result.reason_code == "gpa_below_threshold"
    assert result.evidence_refs == ["cv-1"]


def test_a_rule_that_already_read_a_number_is_not_second_guessed() -> None:
    requirement = _requirement("GPA từ 3.2/4.0 trở lên", threshold=3.2, scale=4.0, operator="gte")
    resume = _resume("GPA: 2.9/4.0")
    rule = evaluate_by_rules(requirement, resume)
    assert rule.reason_code == "gpa_below_threshold"

    result = evaluate_unresolved_requirement(requirement, resume, _readings(_reading(quote="GPA: 2.9/4.0")))

    assert result == rule


def test_a_reading_for_a_span_outside_the_scoped_resume_is_ignored() -> None:
    requirement = _requirement("GPA từ 3.2/4.0 trở lên", threshold=3.2, scale=4.0, operator="gte")
    resume = _resume(TORN)
    rule = evaluate_by_rules(requirement, resume)

    result = evaluate_unresolved_requirement(requirement, resume, _readings(_reading(evidenceId="cv-9")))

    assert result == rule


def test_without_a_reading_the_rule_verdict_stands() -> None:
    requirement = _requirement("GPA từ 3.2/4.0 trở lên", threshold=3.2, scale=4.0, operator="gte")
    resume = _resume(TORN)

    assert evaluate_unresolved_requirement(requirement, resume, _readings(None)) == evaluate_by_rules(
        requirement, resume
    )


def test_a_credential_score_takes_the_same_path() -> None:
    requirement = _requirement("IELTS 6.0 trở lên", kind="language", credential="ielts", threshold=6.0)
    resume = _resume("English: IEL TS 6.5 (2025)", section="languages")
    assert evaluate_by_rules(requirement, resume).reason_code == "language_credential_not_evidenced"
    reading = _reading(kind="credential_score", value=6.5, scale=None, credential="ielts", quote="IEL TS 6.5")

    result = evaluate_unresolved_requirement(requirement, resume, _readings(reading))

    assert result.status == "met"
    assert result.reason_code == "language_credential_evidenced"
    assert result.evidence_refs == ["cv-1"]


# --- candidates: the section is handed over whole ----------------------------------


def test_the_numbers_section_is_handed_to_the_judge_regardless_of_word_overlap() -> None:
    """BM25 cannot rank "GP A: 3.25" for "GPA"; the education section goes in whole."""
    from src.modules.matching.judge.reading_pass import select_candidates

    requirement = _requirement("GPA từ 3.2/4.0 trở lên", threshold=3.2, scale=4.0, operator="gte")
    education = _resume(TORN, "University of Science", section="education")

    with_section = select_candidates(requirement, education, top_k=0, sections=("education",))
    without = select_candidates(requirement, education, top_k=0)

    assert [span.text for span in with_section] == [TORN, "University of Science"]
    assert without == []
