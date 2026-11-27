"""An answered clarification question has to change the verdict.

Four of the ``UnresolvedRequirement`` evaluator families read structured CV
fields and nothing else - the employment timeline, a GPA, a certification list,
a degree.  A clarification answer arrives as an evidence span appended to the
resume, so none of those four ever cited it; ``rescore`` saw an uncited answer
and restored the original row, and the requirement came back ``unknown``
however plainly the candidate had answered.  The generic evaluator did read the
answer, but by design refused a bare claim, so that path ended ``unknown`` too.
Either way the step asked a question, took an answer, and moved nothing - which
is what a reader reported as "it does not score my answer".

What lands instead is a self-report verdict, and only where the evaluator could
not reach one itself: capped confidence, ``candidate_self_report`` provenance,
its own reason code, and no authority to carry a must-have past the eligibility
gate.  A threshold stays a threshold - an answer naming no quantity cannot
clear one - and says so in its reason code rather than going quiet.
"""

from __future__ import annotations

import pytest

from src.modules.matching.application.facade import MatchingFacade
from src.modules.matching.clarifications import rescore as clarification_rescore_module
from src.modules.matching.clarifications.answer_evidence import (
    answer_value_on_scale,
    build_candidate_answer_evidence,
    candidate_self_report_result,
    is_explicit_positive,
    normalize_answer,
    required_experience_months,
)
from src.modules.matching.clarifications.models import (
    CandidateClarificationAnswer,
    MatchClarificationAnalysis,
    MatchClarificationRescoreRequest,
)
from src.modules.matching.clarifications.question_generation import ClarificationQuestion
from src.modules.matching.clarifications.rescore import (
    rescore_match_with_clarification_answers,
)
from src.modules.matching.domain.schemas import MatchRequest, UnresolvedRequirement

SHA256 = "a" * 64
REQUIREMENT_ID = "req-backend-scale"


class StubEmbedder:
    def embed(self, texts):
        return [[0.1, 0.2, 0.3] for _ in texts]


# --------------------------------------------------------------------------
# The policy itself, one requirement shape at a time.
# --------------------------------------------------------------------------


def _requirement(**extra) -> UnresolvedRequirement:
    data = {
        "requirementId": REQUIREMENT_ID,
        "type": "unresolved",
        "sourceEvidenceRef": "jd-ev-1",
        "priority": "nice_to_have",
        "kind": "other",
        "rawLabel": "Requirement under test",
    }
    data.update(extra)
    return UnresolvedRequirement.model_validate(data)


def _verdict(requirement: UnresolvedRequirement, answer_text: str):
    evidence = build_candidate_answer_evidence(REQUIREMENT_ID, answer_text)
    return candidate_self_report_result(requirement, answer_text, evidence)


_MONTHS = _requirement(
    kind="experience",
    rawLabel="At least 3 years of backend development experience",
    minimumExperienceMonths=36,
)
_GPA = _requirement(kind="education", rawLabel="GPA from 3.2/4.0", threshold=3.2, scale=4.0)
_BOOLEAN = _requirement(rawLabel="Có chứng chỉ AWS Solutions Architect Associate")


@pytest.mark.parametrize(
    ("requirement", "answer_text", "status", "reason_code"),
    [
        (_MONTHS, "Tôi có 4 năm kinh nghiệm phát triển backend.", "met",
         "candidate_self_report_meets_requirement"),
        (_MONTHS, "Tôi có 48 tháng kinh nghiệm.", "met",
         "candidate_self_report_meets_requirement"),
        (_MONTHS, "Tôi có 1 năm kinh nghiệm.", "not_met",
         "candidate_self_report_explicitly_below_requirement"),
        (_MONTHS, "Tôi dùng công nghệ này hàng ngày ở dự án hiện tại.", "unknown",
         "candidate_self_report_missing_duration"),
        (_GPA, "GPA của tôi là 3.6/4.0.", "met", "candidate_self_report_meets_requirement"),
        (_GPA, "GPA của tôi là 2.8/4.0.", "not_met",
         "candidate_self_report_explicitly_below_requirement"),
        (_GPA, "Điểm của tôi khá tốt.", "unknown", "candidate_self_report_missing_value"),
        (_BOOLEAN, "Có, tôi đã có chứng chỉ này từ năm 2022.", "met",
         "candidate_self_report_meets_requirement"),
        (_BOOLEAN, "Không, tôi chưa có.", "not_met",
         "candidate_self_report_explicitly_below_requirement"),
        (_BOOLEAN, "abcxyz", "unknown", "candidate_self_report_unclear"),
        (_BOOLEAN, "Chưa chắc lắm.", "unknown", "candidate_self_report_unclear"),
    ],
)
def test_the_self_report_verdict_follows_what_the_answer_actually_claims(
    requirement, answer_text, status, reason_code
) -> None:
    result = _verdict(requirement, answer_text)

    assert result is not None
    assert result.status == status
    assert result.reason_code == reason_code
    assert result.confidence <= 0.72
    assert result.evidence_sources[result.evidence_refs[0]] == "candidate_self_report"
    assert "chưa được xác minh độc lập" in (result.evidence_explanation or "")


def test_a_threshold_that_cannot_be_checked_says_which_threshold_it_was() -> None:
    """Silence was the complaint, so the unscorable case has to explain itself."""

    result = _verdict(_MONTHS, "Tôi dùng công nghệ này hàng ngày.")

    assert result is not None
    assert "36" in (result.evidence_explanation or "")


@pytest.mark.parametrize(
    ("text", "scale", "expected"),
    [
        ("GPA 3.6/4.0", 4.0, 3.6),
        ("GPA của tôi là 3.6", 4.0, 3.6),
        ("7.2/10", 4.0, pytest.approx(2.88)),
        ("Tôi có 2 năm kinh nghiệm", None, None),
        ("Điểm khá tốt", 4.0, None),
    ],
)
def test_a_graded_answer_is_read_back_onto_the_posting_scale(text, scale, expected) -> None:
    assert answer_value_on_scale(text, scale) == expected


def test_the_month_threshold_is_found_wherever_the_requirement_states_it() -> None:
    assert required_experience_months(_MONTHS) == 36
    assert required_experience_months(
        _requirement(kind="experience", rawLabel="At least 2 years of QA experience")
    ) == 24
    assert required_experience_months(_BOOLEAN) is None


# --------------------------------------------------------------------------
# End to end: the same answer, through rescore, on a requirement that really
# does start from ``unknown``.
# --------------------------------------------------------------------------


def _evidence(evidence_id: str, document_id: str, text: str) -> dict:
    return {
        "evidenceId": evidence_id,
        "documentId": document_id,
        "documentSha256": SHA256,
        "section": "experience",
        "text": text,
        "charStart": 0,
        "charEnd": len(text),
        "page": 1,
    }


def _request(*, priority: str = "nice_to_have") -> MatchRequest:
    return MatchRequest.model_validate(
        {
            "schemaVersion": "2.1",
            "resume": {
                "schemaVersion": "2.1",
                "resumeId": "cv-1",
                "documentId": "cv-doc-1",
                "documentSha256": SHA256,
                "evidence": [
                    _evidence("cv-ev-backend", "cv-doc-1", "Developed Spring Boot backend services.")
                ],
            },
            "job": {
                "schemaVersion": "2.1",
                "jobId": "job-1",
                "documentId": "job-doc-1",
                "documentSha256": SHA256,
                "evidence": [
                    _evidence(
                        "jd-ev-backend",
                        "job-doc-1",
                        "Strong experience designing scalable backend systems.",
                    )
                ],
                "requirements": [
                    {
                        "requirementId": REQUIREMENT_ID,
                        "type": "unresolved",
                        "kind": "experience",
                        "priority": priority,
                        "sourceEvidenceRef": "jd-ev-backend",
                        "rawLabel": "Strong experience designing scalable backend systems",
                    }
                ],
            },
            "matchingPolicy": {"policyVersion": "balanced-v1", "bm25ProviderMode": "in_memory"},
            "asyncProcessing": False,
        }
    )


async def _answer(request: MatchRequest, answer_text: str, monkeypatch):
    monkeypatch.setattr(
        clarification_rescore_module,
        "get_matching_facade",
        lambda: MatchingFacade(StubEmbedder()),
    )
    match = MatchingFacade(StubEmbedder()).match(request)
    assert match.requirement_results[0].status == "unknown", "fixture must start from unknown"
    analysis = MatchClarificationAnalysis(
        matchResult=match,
        clarificationRequests=[
            ClarificationQuestion(
                requirementId=REQUIREMENT_ID,
                missingDimension="experience_context",
                confidence=0.9,
                evidenceRefs=[],
                questionText="Bạn đã trực tiếp phụ trách phần việc nào liên quan tới hệ thống backend?",
                semanticAlignmentScore=0.9,
            )
        ],
    )
    payload = MatchClarificationRescoreRequest(
        matchRequest=request,
        initialAnalysis=analysis,
        answers=[
            CandidateClarificationAnswer(requirementId=REQUIREMENT_ID, answerText=answer_text)
        ],
    )
    response = await rescore_match_with_clarification_answers(payload)
    return response, response.final_match_result.requirement_results[0]


@pytest.mark.asyncio
async def test_a_plain_yes_the_evaluator_refuses_is_still_recorded_as_an_answer(
    monkeypatch,
) -> None:
    """The reply that used to leave ``unknown -> unknown`` now lands somewhere.

    The generic evaluator will not promote a bare claim, and it is right not
    to: a claim is not a description of having done the work. But refusing it
    there left the requirement in the exact state the clarification step exists
    to clear, so the answer is recorded for what it is - the candidate's own
    word, capped and labelled - rather than discarded.
    """

    response, result = await _answer(_request(), "Có, tôi làm việc này thường xuyên.", monkeypatch)

    assert result.status == "met"
    assert result.reason_code == "candidate_self_report_meets_requirement"
    assert result.evidence_sources[result.evidence_refs[0]] == "candidate_self_report"
    assert response.processed_answers[0].status == "met"
    assert "candidate_self_report_used" in response.final_match_result.warnings


@pytest.mark.asyncio
async def test_an_answer_describing_the_work_keeps_the_evaluator_verdict(monkeypatch) -> None:
    """Where the evaluator can read the answer, the evaluator still decides."""

    _, result = await _answer(
        _request(),
        "Tôi đã trực tiếp thiết kế và mở rộng hệ thống backend cho cổng khách hàng.",
        monkeypatch,
    )

    assert result.status == "met"
    assert result.reason_code != "candidate_self_report_meets_requirement"


@pytest.mark.asyncio
async def test_gibberish_credits_nothing(monkeypatch) -> None:
    response, result = await _answer(_request(), "abcxyz", monkeypatch)

    assert result.status == "unknown"
    assert result.reason_code == "candidate_self_report_unclear"
    assert "candidate_self_report_used" not in response.final_match_result.warnings


@pytest.mark.asyncio
async def test_saying_no_is_still_an_answer_of_no(monkeypatch) -> None:
    _, result = await _answer(_request(), "Không, tôi chưa có kinh nghiệm này.", monkeypatch)

    assert result.status == "not_met"
    assert result.reason_code == "candidate_self_report_explicitly_below_requirement"


@pytest.mark.asyncio
async def test_a_self_reported_must_have_goes_to_review_not_straight_through(monkeypatch) -> None:
    """Self-report counts, but it may not be what clears the must-have gate.

    A candidate typing "có" into their own assessment is the one input the
    eligibility decision cannot take at face value, so the requirement reads
    ``met`` and the overall decision still asks for a human.
    """

    response, result = await _answer(
        _request(priority="must_have"), "Có, tôi làm việc này thường xuyên.", monkeypatch
    )

    assert result.status == "met"
    assert response.final_match_result.eligibility == "review_required"
    assert REQUIREMENT_ID not in response.final_match_result.failed_must_have_requirements


# A reader answered "Bạn đã ra trường chưa?" with "Dạ rồi" and was told the
# answer did not say anything.  Two causes, both in the wording lists:
# ``normalize_answer`` left ``đ`` standing, so every entry spelled with one
# ("đã sử dụng", "chưa đạt") could never match; and the Vietnamese side of the
# yes list held only "có", while an ordinary Vietnamese yes is "rồi", "vâng",
# "dạ rồi", "đúng rồi", "tốt nghiệp rồi".
_YES = "candidate_self_report_meets_requirement"
_NO = "candidate_self_report_explicitly_below_requirement"
_UNCLEAR = "candidate_self_report_unclear"

_GRADUATED = _requirement(kind="education", rawLabel="Yêu cầu ứng viên đã ra trường")


@pytest.mark.parametrize(
    ("answer_text", "reason_code"),
    [
        ("Dạ rồi", _YES),
        ("Dạ rồi.", _YES),
        ("Rồi ạ", _YES),
        ("Vâng, tôi đã tốt nghiệp.", _YES),
        ("Tôi đã ra trường rồi.", _YES),
        ("Tốt nghiệp rồi ạ", _YES),
        ("Dạ có", _YES),
        ("Đúng rồi", _YES),
        ("Chính xác.", _YES),
        ("Chắc chắn rồi.", _YES),
        ("Yes, I graduated last year.", _YES),
        ("Dạ chưa", _NO),
        ("Dạ, chưa ạ.", _NO),
        ("Chưa ạ", _NO),
        ("Không.", _NO),
        ("Tôi chưa tốt nghiệp.", _NO),
        ("Tôi chưa ra trường.", _NO),
        ("Not yet.", _NO),
        ("Chưa chắc lắm.", _UNCLEAR),
        ("Tuỳ trường hợp.", _UNCLEAR),
    ],
)
def test_an_ordinary_vietnamese_yes_or_no_is_read_as_one(answer_text, reason_code) -> None:
    result = _verdict(_GRADUATED, answer_text)

    assert result is not None, answer_text
    assert result.reason_code == reason_code, answer_text


def test_a_letter_with_a_stroke_does_not_hide_the_phrase_it_belongs_to() -> None:
    """``đ`` has no NFKD decomposition, so it survived the accent strip."""

    assert normalize_answer("Đã sử dụng được 3 năm") == "da su dung duoc 3 nam"
    assert normalize_answer("Chưa đạt") == "chua dat"


def test_a_negator_keeps_the_word_after_it_from_reading_as_agreement() -> None:
    assert is_explicit_positive("Chắc chắn rồi.") is True
    assert is_explicit_positive("Chưa chắc chắn.") is False
    assert is_explicit_positive("Tôi không có kinh nghiệm.") is False
