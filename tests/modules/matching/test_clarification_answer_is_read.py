"""The answer to a clarification question has to reach the evaluator.

A clarification answer is appended to the resume as an evidence span and the
match is re-run; ``rescore`` then keeps the new verdict only if the evaluator
actually cited that span, and restores the original result otherwise.  The
generic evaluator ranked every span by word overlap with the posting's wording,
so a Vietnamese reply to an English requirement shared no words with it, was
never cited, and was dropped as unused - the system asked a question and
ignored the answer, leaving ``unknown -> unknown`` with
``self_report_used: false`` in the run trace.

The span carries the id of the requirement it answers, so its relevance is a
fact about the span, not something to re-derive from its wording.
"""

from __future__ import annotations

import pytest

from src.modules.matching.clarifications.answer_evidence import (
    build_candidate_answer_evidence,
)
from src.modules.matching.domain.schemas import UnresolvedRequirement
from src.modules.matching.evaluation.requirement_evaluators import (
    evaluate_unresolved_requirement,
)
from src.modules.user_cvs.schemas import CanonicalResume

SHA = "c" * 64
REQUIREMENT_ID = "req-other-1280"
LABEL = "Strong debugging, problem-solving, and performance optimization skills"
CV_LINE = "Built REST APIs with Java, Spring Boot and PostgreSQL."


def _requirement(requirement_id: str = REQUIREMENT_ID) -> UnresolvedRequirement:
    return UnresolvedRequirement.model_validate(
        {
            "requirementId": requirement_id,
            "type": "unresolved",
            "sourceEvidenceRef": "jd-ev-1",
            "kind": "other",
            "priority": "nice_to_have",
            "rawLabel": LABEL,
        }
    )


def _resume_without_answer() -> CanonicalResume:
    return CanonicalResume.model_validate(
        {
            "schemaVersion": "2.1",
            "resumeId": "resume-answer-fixture",
            "documentId": "resume-answer-fixture",
            "documentSha256": SHA,
            "evidence": [
                {
                    "evidenceId": "cv-1",
                    "documentId": "resume-answer-fixture",
                    "documentSha256": SHA,
                    "section": "projects",
                    "text": CV_LINE,
                    "charStart": 0,
                    "charEnd": len(CV_LINE),
                    "page": 1,
                }
            ],
        }
    )


def _resume_with_answer(answer: str, *, answered_requirement_id: str = REQUIREMENT_ID):
    answer_evidence = build_candidate_answer_evidence(answered_requirement_id, answer)
    resume = CanonicalResume.model_validate(
        {
            "schemaVersion": "2.1",
            "resumeId": "resume-answer-fixture",
            "documentId": "resume-answer-fixture",
            "documentSha256": SHA,
            "evidence": [
                {
                    "evidenceId": "cv-1",
                    "documentId": "resume-answer-fixture",
                    "documentSha256": SHA,
                    "section": "projects",
                    "text": CV_LINE,
                    "charStart": 0,
                    "charEnd": len(CV_LINE),
                    "page": 1,
                }
            ],
        }
    )
    return (
        resume.model_copy(update={"evidence": [*resume.evidence, answer_evidence]}),
        answer_evidence,
    )


@pytest.mark.parametrize(
    "answer",
    [
        "Tôi đã từng tối ưu truy vấn chậm và sửa lỗi trong hệ thống backend.",
        "Có, tôi làm việc này thường xuyên.",
        "I have strong debugging and performance optimization skills.",
    ],
)
def test_the_answer_is_always_cited_whatever_words_it_uses(answer):
    """Citation is what stops ``rescore`` discarding the answer as unused."""

    resume, answer_evidence = _resume_with_answer(answer)
    result = evaluate_unresolved_requirement(_requirement(), resume)

    assert answer_evidence.evidence_id in result.evidence_refs


@pytest.mark.parametrize(
    "answer",
    [
        "Tôi đã từng tối ưu truy vấn chậm và sửa lỗi trong hệ thống backend.",
        "Tôi có kinh nghiệm debug và tối ưu hiệu năng.",
        "Tôi trực tiếp phụ trách việc khắc phục lỗi hiệu năng của hệ thống.",
    ],
)
def test_an_answer_describing_real_work_resolves_the_requirement(answer):
    resume, _ = _resume_with_answer(answer)
    result = evaluate_unresolved_requirement(_requirement(), resume)

    assert result.status == "met"
    assert result.reason_code == "generic_requirement_evidenced"


@pytest.mark.parametrize(
    "answer",
    [
        # Asserting that one has a skill is not describing having done it.
        "Có, tôi làm việc này thường xuyên.",
        "Dạ có ạ.",
        "Tôi biết cái này.",
        # Echoing the posting's own wording back is still only a claim: it must
        # not score where the same claim in Vietnamese does not.
        "I have strong debugging and performance optimization skills.",
        # Not an answer at all.
        "abcxyz",
        "Tôi không biết.",
    ],
)
def test_a_bare_claim_is_read_but_leaves_the_verdict_where_the_cv_left_it(answer):
    """Citing an answer must not, by itself, move the verdict.

    ``unknown`` rows are left out of the score, so letting any answer push a
    requirement from ``not_met`` to ``unknown`` meant that typing "tôi không
    biết", or a line of gibberish, raised the score by as much as a real
    answer did.
    """

    without_answer = evaluate_unresolved_requirement(_requirement(), _resume_without_answer())
    resume, answer_evidence = _resume_with_answer(answer)
    result = evaluate_unresolved_requirement(_requirement(), resume)

    assert result.status == without_answer.status
    assert result.score == without_answer.score
    # Cited even so, so the reader can see exactly what the system read.
    assert answer_evidence.evidence_id in result.evidence_refs


def test_an_answer_about_another_requirement_is_not_borrowed():
    """The id on the span is what admits it, so another one's answer cannot."""

    resume, answer_evidence = _resume_with_answer(
        "Tôi đã từng tối ưu truy vấn chậm và sửa lỗi trong hệ thống backend.",
        answered_requirement_id="req-other-9999",
    )
    result = evaluate_unresolved_requirement(_requirement(), resume)

    assert answer_evidence.evidence_id not in result.evidence_refs
    assert result.status != "met"


def test_a_cv_passage_is_scored_by_the_cv_rules_not_the_answer_rules():
    """The wider answer vocabulary must not leak into CV scoring.

    The same Vietnamese sentence as a CV line stays what it was before this
    change, so the CV-side measurements in the paper remain comparable.
    """

    text = "Tối ưu truy vấn và sửa lỗi hiệu năng."
    resume = CanonicalResume.model_validate(
        {
            "schemaVersion": "2.1",
            "resumeId": "resume-cv-only",
            "documentId": "resume-cv-only",
            "documentSha256": SHA,
            "evidence": [
                {
                    "evidenceId": "cv-1",
                    "documentId": "resume-cv-only",
                    "documentSha256": SHA,
                    "section": "skills",
                    "text": text,
                    "charStart": 0,
                    "charEnd": len(text),
                    "page": 1,
                }
            ],
        }
    )
    result = evaluate_unresolved_requirement(_requirement(), resume)

    assert result.status != "met"
