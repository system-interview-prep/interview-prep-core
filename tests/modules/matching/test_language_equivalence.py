"""An English requirement answered by a different English test.

"IELTS 6.0+ hoặc tương đương" asks for a level, and a candidate holding
TOEIC 900 holds it.  The evaluator compared the *name* of the test with a
string equality, so that candidate was reported as having no evidence at all -
"Không tìm thấy", which is ``not_met`` with no citation, and for a must-have
criterion that is an outright rejection with nothing to appeal to.
"""

from __future__ import annotations

import pytest

from src.modules.matching.domain.schemas import UnresolvedRequirement
from src.modules.matching.evaluation.language_equivalence import (
    cefr_level_for_score,
    cefr_level_in_text,
    scale_is_unambiguous,
)
from src.modules.matching.evaluation.requirement_evaluators import (
    evaluate_unresolved_requirement,
)
from src.modules.user_cvs.schemas import CanonicalResume

SHA = "c" * 64


def _resume(*texts: str) -> CanonicalResume:
    return CanonicalResume.model_validate(
        {
            "schemaVersion": "2.1",
            "resumeId": "resume-language-fixture",
            "documentId": "resume-language-fixture",
            "documentSha256": SHA,
            "evidence": [
                {
                    "evidenceId": f"cv-{index}",
                    "documentId": "resume-language-fixture",
                    "documentSha256": SHA,
                    "section": "certifications",
                    "text": text,
                    "charStart": 0,
                    "charEnd": len(text),
                    "page": 1,
                }
                for index, text in enumerate(texts)
            ],
        }
    )


def _requirement(
    raw_label: str = "IELTS 6.0+ hoặc tương đương",
    *,
    credential: str = "IELTS",
    threshold: float | None = 6.0,
    equivalent_allowed: bool | None = True,
) -> UnresolvedRequirement:
    return UnresolvedRequirement.model_validate(
        {
            "requirementId": "req-language",
            "type": "unresolved",
            "sourceEvidenceRef": "jd-ev-1",
            "kind": "language",
            "priority": "must_have",
            "rawLabel": raw_label,
            "credential": credential,
            "threshold": threshold,
            "operator": "gte",
            "equivalentAllowed": equivalent_allowed,
        }
    )


# --------------------------------------------------------------- the table --


@pytest.mark.parametrize(
    ("credential", "score", "level"),
    [
        ("ielts", 6.0, "B2"),
        ("ielts", 5.0, "B1"),
        ("ielts", 7.0, "C1"),
        ("toeic", 900.0, "B2"),
        ("toeic", 400.0, "A2"),
        ("toeic", 950.0, "C1"),
        ("toefl", 95.0, "C1"),
        ("toefl", 72.0, "B2"),
    ],
)
def test_published_cefr_bands(credential, score, level):
    assert cefr_level_for_score(credential, score) == level


@pytest.mark.parametrize(
    ("credential", "score"),
    [
        # Not English: a JLPT level says nothing about an IELTS requirement.
        ("jlpt", 2.0),
        ("hsk", 5.0),
        # Off the test's own scale: "TOEIC 2026" is a year.
        ("toeic", 2026.0),
        ("ielts", 11.0),
    ],
)
def test_scores_that_cannot_be_placed_are_not_placed(credential, score):
    assert cefr_level_for_score(credential, score) is None


def test_a_stated_level_needs_an_anchor_in_front_of_it():
    assert cefr_level_in_text("english cefr b1") == "B1"
    assert cefr_level_in_text("tieng anh b2") == "B2"
    # "B1" on its own is a bullet, a grade, a room number - not a CEFR level.
    assert cefr_level_in_text("duong b1 quan 7") is None


def test_toeic_speaking_and_writing_is_a_different_scale():
    assert scale_is_unambiguous("toeic", "toeic 900 lr") is True
    assert scale_is_unambiguous("toeic", "toeic s w 350") is False
    # The guard is about TOEIC's two tests and must not touch the others.
    assert scale_is_unambiguous("ielts", "ielts s w 7.0") is True


# ------------------------------------------------------------- the verdict --


def test_toeic_900_answers_an_ielts_6_requirement_that_allows_equivalents():
    result = evaluate_unresolved_requirement(
        _requirement(), _resume("CERTIFICATIONS TOEIC: 900 (LR) 2026")
    )

    assert result.status == "met"
    assert result.reason_code == "language_credential_equivalent"
    assert result.evidence_refs == ["cv-0"]


def test_an_equivalent_without_the_posting_s_permission_is_a_question_not_a_rejection():
    """The posting named one test and did not say another would do.

    The candidate reaches the level on a different test, which the employer may
    or may not accept.  That is a question, and leaving it ``unknown`` is what
    puts a clarification in front of the reader; ``not_met`` ended the matter
    with no evidence cited at all.
    """

    result = evaluate_unresolved_requirement(
        _requirement("IELTS 6.0", equivalent_allowed=None),
        _resume("CERTIFICATIONS TOEIC: 900 (LR) 2026"),
    )

    assert result.status == "unknown"
    assert result.reason_code == "language_credential_equivalence_unconfirmed"
    assert result.evidence_refs == ["cv-0"]


def test_short_of_the_level_fails_whatever_the_posting_allows():
    result = evaluate_unresolved_requirement(_requirement(), _resume("TOEIC: 400 2026"))

    assert result.status == "not_met"
    assert result.reason_code == "language_credential_below_minimum"
    # Still cited, so the candidate can see what was read.
    assert result.evidence_refs == ["cv-0"]


@pytest.mark.parametrize(
    ("cv_line", "status"),
    [
        ("IELTS 6.5", "met"),
        ("IELTS Academic 6.5", "met"),
        ("IELTS 5.0", "not_met"),
        ("TOEFL iBT 95", "met"),
        ("TOEIC L&R: 900", "met"),
        ("TOEIC 990", "met"),
        ("English: CEFR B1", "not_met"),
    ],
)
def test_credential_variants_are_read_rather_than_skipped(cv_line, status):
    assert evaluate_unresolved_requirement(_requirement(), _resume(cv_line)).status == status


def test_the_same_test_is_compared_on_its_raw_score_not_its_band():
    """IELTS 6.0 and 6.5 are both B2, so CEFR cannot separate them.

    A same-test comparison must therefore never route through the table.
    """

    result = evaluate_unresolved_requirement(
        _requirement(threshold=6.5), _resume("IELTS 6.0")
    )

    assert result.status == "not_met"
    assert result.reason_code == "language_credential_below_minimum"


@pytest.mark.parametrize(
    "cv_line",
    [
        # A Japanese credential is not a statement about English.
        "JLPT N2",
        "HSK 5",
        # A year rather than a score.
        "TOEIC 2026",
        # A Speaking & Writing result sits on a 0-400 scale this table does
        # not describe, so it must not be placed against the 0-990 bands.
        "TOEIC S&W 350",
        "No certificates. Python, Java, Docker",
    ],
)
def test_nothing_outside_the_published_tables_is_converted(cv_line):
    result = evaluate_unresolved_requirement(_requirement(), _resume(cv_line))

    assert result.reason_code in {
        "language_credential_not_evidenced",
        "language_credential_evidence_missing",
    }
    assert result.evidence_refs == []
