"""The judge reads a clarification answer beside its question - and only there.

A reply of "Dạ rồi" to "Bạn đã ra trường chưa?" reached the judge as a bare
"Dạ rồi": the answer span carried no question, so the model was asked whether
two words entail graduation and said, correctly, that it could not tell.  The
question now travels with the answer on the rescore path, as a field beside
the reply rather than inside it.

What must not move is the first-pass CV-JD match.  Every hook below is a
keyword argument defaulting to None: no schema gains a field, so request
bodies and both cache keys are unchanged, and a match that passes nothing
calls the judge with the same instructions and the same input as before -
even when the resume already carries an answer span.

And what the judge may do with the question is bounded: it is context, never
evidence.  A quote lifted from the question fails grounding, because the
check still runs against the reply alone; and a must-have the judge clears
from the reply still goes to review, because the gate now reads provenance,
not the reason code.
"""

from __future__ import annotations

import json

import pytest

from src.core.config import Settings, get_settings
from src.modules.matching.application.facade import MatchingFacade
from src.modules.matching.clarifications import rescore as clarification_rescore_module
from src.modules.matching.clarifications.models import (
    CandidateClarificationAnswer,
    MatchClarificationAnalysis,
    MatchClarificationRescoreRequest,
)
from src.modules.matching.clarifications.question_generation import ClarificationQuestion
from src.modules.matching.clarifications.rescore import rescore_match_with_clarification_answers
from src.modules.matching.domain.schemas import CanonicalJob, MatchRequest, RequirementResult
from src.modules.matching.judge import reading_pass
from src.modules.matching.judge.grounded_judge import (
    _ASKED_ADDENDUM,
    _INSTRUCTIONS,
    GroundedJudge,
    build_input,
)
from src.modules.matching.judge.reading_pass import build_reading_set
from src.modules.user_cvs.schemas import CanonicalResume, EvidenceSpan


class StubEmbedder:
    def embed(self, texts):
        return [[0.1, 0.2, 0.3] for _ in texts]


SHA = "c" * 64
REQ = "req-graduated"
QUESTION = "Bạn đã ra trường chưa?"
ANSWER = "Dạ rồi"


def _span(
    evidence_id: str, text: str, *, section: str = "education", answers: str | None = None
) -> EvidenceSpan:
    item = {
        "evidenceId": evidence_id,
        "documentId": "cv-1",
        "documentSha256": SHA,
        "section": section,
        "text": text,
        "charStart": 0,
        "charEnd": len(text),
    }
    if answers is not None:
        item["evidenceSource"] = "candidate_self_report"
        item["sourceRequirementId"] = answers
    return EvidenceSpan.model_validate(item)


# A CV that leaves the question open, which is what gets it asked: the
# shared words make the rule side call the evidence weak rather than absent.
DOCUMENT = _span("cv-edu", "Sinh viên Đại học Bách Khoa Hà Nội, dự kiến ra trường năm 2026.")
REPLY = _span("ans-1", ANSWER, section="candidate_self_report", answers=REQ)


class RecordingClient:
    """Answers by content: a satisfies for the reply once it comes with its question."""

    def __init__(self, *, quote: str | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._quote = quote

    def complete(self, *, instructions: str, input_text: str, output_schema: dict) -> str:
        payload = json.loads(input_text)
        self.calls.append((instructions, payload))
        asked = [item for item in payload["excerpts"] if "asked" in item]
        if asked:
            # Cite the reply by the id it was handed, quoting its text - or the
            # words a test wants tried instead.
            reply = asked[0]
            quote = self._quote if self._quote is not None else reply["text"]
            return json.dumps(
                {
                    "relation": "satisfies",
                    "selected": [{"evidence_id": reply["evidence_id"], "quote": quote}],
                },
                ensure_ascii=False,
            )
        return json.dumps({"relation": "unsure", "selected": []})


# --------------------------------------------------------------------------
# build_input: the first-pass format is pinned, byte for byte.
# --------------------------------------------------------------------------


def test_without_a_question_the_judge_input_is_the_first_pass_format_exactly() -> None:
    expected = json.dumps(
        {
            "requirement": "Đã ra trường",
            "excerpts": [
                {"evidence_id": "cv-edu", "section": "education", "text": DOCUMENT.text},
                {"evidence_id": "ans-1", "section": "candidate_self_report", "text": ANSWER},
            ],
        },
        ensure_ascii=False,
    )

    assert build_input("Đã ra trường", [DOCUMENT, REPLY]) == expected


def test_the_question_sits_beside_the_reply_and_touches_no_document_span() -> None:
    payload = json.loads(build_input("Đã ra trường", [DOCUMENT, REPLY], asked=QUESTION))

    document, reply = payload["excerpts"]
    assert "asked" not in document
    assert reply["asked"] == QUESTION
    assert reply["text"] == ANSWER, "the reply itself is untouched"


# --------------------------------------------------------------------------
# GroundedJudge: the question is context, never evidence.
# --------------------------------------------------------------------------


def test_the_instructions_change_only_when_a_question_is_handed_over() -> None:
    client = RecordingClient()
    judge = GroundedJudge(client)

    judge.judge(requirement_id=REQ, requirement_text="x", candidates=[DOCUMENT, REPLY])
    judge.judge(requirement_id=REQ, requirement_text="x", candidates=[DOCUMENT, REPLY], asked=QUESTION)

    assert client.calls[0][0] == _INSTRUCTIONS
    assert client.calls[1][0] == _INSTRUCTIONS + _ASKED_ADDENDUM


def test_a_quote_lifted_from_the_question_is_not_grounded() -> None:
    """The system wrote the question; its words cannot stand as the candidate's."""
    judge = GroundedJudge(RecordingClient(quote="ra trường"))

    outcome = judge.judge(
        requirement_id=REQ, requirement_text="x", candidates=[DOCUMENT, REPLY], asked=QUESTION
    )

    assert outcome.reading is not None
    assert outcome.reading.relation == "unsure"
    assert outcome.reading.dropped == {"ans-1": "quote_not_in_span"}
    assert outcome.fallback == "no_valid_span"


def test_a_quote_from_the_reply_is_grounded() -> None:
    judge = GroundedJudge(RecordingClient())

    outcome = judge.judge(
        requirement_id=REQ, requirement_text="x", candidates=[DOCUMENT, REPLY], asked=QUESTION
    )

    assert outcome.reading is not None
    assert outcome.reading.relation == "satisfies"
    assert [item.evidence_id for item in outcome.reading.selected] == ["ans-1"]


# --------------------------------------------------------------------------
# build_reading_set: a first-pass match never hands the judge a question,
# even when the resume already holds an answer span.
# --------------------------------------------------------------------------


def _resume(*spans: EvidenceSpan) -> CanonicalResume:
    return CanonicalResume.model_validate(
        {
            "schemaVersion": "2.1",
            "resumeId": "cv-1",
            "documentId": "cv-1",
            "documentSha256": SHA,
            "evidence": [span.model_dump(by_alias=True, exclude_none=True) for span in spans],
            "skills": [],
            "languages": [],
            "employment": [],
            "education": [],
            "projects": [],
            "certifications": [],
        }
    )


def _job(*, priority: str = "nice_to_have") -> CanonicalJob:
    return CanonicalJob.model_validate(
        {
            "schemaVersion": "2.1",
            "jobId": "job-1",
            "documentId": "job-1",
            "documentSha256": SHA,
            "jobTitle": "Backend Engineer",
            "requirements": [
                {
                    "requirementId": REQ,
                    "type": "unresolved",
                    "sourceEvidenceRef": "jd-ev-1",
                    "kind": "education",
                    "priority": priority,
                    "rawLabel": "Yêu cầu ứng viên đã ra trường",
                }
            ],
            "evidence": [
                {
                    "evidenceId": "jd-ev-1",
                    "documentId": "job-1",
                    "documentSha256": SHA,
                    "section": "requirements",
                    "text": "Yêu cầu ứng viên đã ra trường.",
                    "charStart": 0,
                    "charEnd": len("Yêu cầu ứng viên đã ra trường."),
                }
            ],
        }
    )


def _judge_settings() -> Settings:
    return Settings(matching_judge_enabled=True, matching_judge_top_k=8, openai_api_key=None)


def test_a_first_pass_match_passes_no_question_even_with_an_answer_span_present() -> None:
    client = RecordingClient()

    build_reading_set(
        _resume(DOCUMENT, REPLY), _job(), judge=GroundedJudge(client), settings=_judge_settings()
    )

    (instructions, payload), = client.calls
    assert instructions == _INSTRUCTIONS
    assert all("asked" not in item for item in payload["excerpts"])


def test_the_rescore_pass_attaches_each_question_to_its_own_requirement_only() -> None:
    client = RecordingClient()

    build_reading_set(
        _resume(DOCUMENT, REPLY),
        _job(),
        judge=GroundedJudge(client),
        settings=_judge_settings(),
        clarification_prompts={REQ: QUESTION, "req-other": "Một câu hỏi khác?"},
    )

    (_, payload), = client.calls
    by_id = {item["evidence_id"]: item for item in payload["excerpts"]}
    assert by_id["ans-1"]["asked"] == QUESTION
    assert "asked" not in by_id["cv-edu"]


# --------------------------------------------------------------------------
# The gate reads provenance.  Reachable today: a long enough reply already
# let the judge clear a must-have under ``requirement_judged_satisfied``.
# --------------------------------------------------------------------------


def _request(*, priority: str = "nice_to_have") -> MatchRequest:
    return MatchRequest.model_validate(
        {
            "schemaVersion": "2.1",
            "resume": _resume(DOCUMENT).model_dump(by_alias=True),
            "job": _job(priority=priority).model_dump(by_alias=True),
            "matchingPolicy": {"policyVersion": "balanced-v1", "bm25ProviderMode": "in_memory"},
            "asyncProcessing": False,
        }
    )


def _judged_met(source: str) -> RequirementResult:
    return RequirementResult(
        requirementId=REQ,
        status="met",
        score=1.0,
        confidence=0.85,
        evidenceRefs=["x"],
        evidenceSources={"x": source},
        reasonCode="requirement_judged_satisfied",
        evidenceExplanation="",
    )


def test_a_must_have_the_judge_clears_from_the_reply_still_goes_to_review() -> None:
    payload = _request(priority="must_have")

    assert MatchingFacade._eligibility(payload, [_judged_met("candidate_self_report")]) == "review_required"
    assert MatchingFacade._eligibility(payload, [_judged_met("document")]) == "eligible"


# --------------------------------------------------------------------------
# End to end through rescore, with a scripted judge.
# --------------------------------------------------------------------------


async def _rescore(answer_text: str, monkeypatch, *, trusted: bool, priority: str = "nice_to_have"):
    client = RecordingClient()
    monkeypatch.setattr(get_settings(), "matching_judge_enabled", True)
    monkeypatch.setattr(reading_pass, "default_judge", lambda _settings: GroundedJudge(client))
    monkeypatch.setattr(reading_pass, "default_numeric_judge", lambda _settings: None)
    monkeypatch.setattr(
        clarification_rescore_module, "get_matching_facade", lambda: MatchingFacade(StubEmbedder())
    )
    request = _request(priority=priority)
    match = MatchingFacade(StubEmbedder()).match(request)
    assert match.requirement_results[0].status == "unknown", "fixture must start from unknown"
    client.calls.clear()  # that check was a fourth match; count the rescore's three alone
    analysis = MatchClarificationAnalysis(
        matchResult=match,
        clarificationRequests=[
            ClarificationQuestion(
                requirementId=REQ,
                missingDimension="education",
                confidence=0.9,
                evidenceRefs=["cv-edu"],
                questionText=QUESTION,
                semanticAlignmentScore=0.9,
            )
        ],
    )
    payload = MatchClarificationRescoreRequest(
        matchRequest=request,
        initialAnalysis=analysis,
        answers=[CandidateClarificationAnswer(requirementId=REQ, answerText=answer_text)],
    )
    response = await rescore_match_with_clarification_answers(
        payload, trusted_questions={REQ: QUESTION} if trusted else None
    )
    return response, response.final_match_result.requirement_results[0], client


@pytest.mark.asyncio
async def test_da_roi_with_its_question_is_read_by_the_judge_as_a_yes(monkeypatch) -> None:
    response, result, client = await _rescore(ANSWER, monkeypatch, trusted=True)

    assert result.status == "met"
    assert result.reason_code == "requirement_judged_satisfied"
    assert list(result.evidence_sources.values()) == ["candidate_self_report"]
    assert result.confidence <= 0.72, "a reply is never as sure as a CV"
    assert (result.evidence_explanation or "").startswith("Câu trả lời của ứng viên")
    assert "candidate_self_report_used" in response.final_match_result.warnings
    # Three passes; only the one holding the answer carried the question.
    asked = [any("asked" in item for item in payload["excerpts"]) for _, payload in client.calls]
    assert asked == [False, True, False]


@pytest.mark.asyncio
async def test_without_a_trusted_question_the_judge_sees_what_it_saw_before(monkeypatch) -> None:
    """The body's own question is the client's word; the judge is not shown it."""
    _, result, client = await _rescore(ANSWER, monkeypatch, trusted=False)

    assert all(not any("asked" in item for item in payload["excerpts"]) for _, payload in client.calls)
    assert all(instructions == _INSTRUCTIONS for instructions, _ in client.calls)
    # The rule floor still reads the plain yes; nothing is lost, only the judge's say.
    assert result.status == "met"
    assert result.reason_code == "candidate_self_report_meets_requirement"


@pytest.mark.asyncio
async def test_a_must_have_cleared_by_the_judge_from_a_reply_is_reviewed(monkeypatch) -> None:
    response, result, _ = await _rescore(ANSWER, monkeypatch, trusted=True, priority="must_have")

    assert result.status == "met"
    assert result.reason_code == "requirement_judged_satisfied"
    assert response.final_match_result.eligibility == "review_required"
    assert REQ not in response.final_match_result.failed_must_have_requirements
