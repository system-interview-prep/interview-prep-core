import hashlib
import json

import httpx
import pytest

from src.modules.matching.clarifications.ambiguity import (
    JevAmbiguityAnalyzer,
    NoopAmbiguityAnalyzer,
    build_clarification_requests,
)
from src.modules.matching.clarifications import (
    CandidateClarificationAnswer,
    MatchClarificationAnalysis,
    MatchClarificationRescoreRequest,
    rescore_match_with_clarification_answers,
)
from src.modules.matching.clarifications.question_generation import (
    ClarificationPlan,
    ClarificationQuestion,
    ClarificationQuestionService,
    GeneratedQuestion,
)
from src.modules.matching.clarifications import rescore as clarification_rescore_module
from src.modules.matching.clarifications import router as clarification_router_module
from src.modules.matching.clarifications import service as clarification_module
from src.modules.matching.clarifications.service import _encode_clarification_plans
from src.modules.matching.clarifications.models import (
    MatchClarificationIdsRequest,
    MatchClarificationRescoreIdsRequest,
    MatchClarificationRescoreResult,
)
from src.modules.matching.application.facade import MatchingFacade
from src.modules.matching.domain.schemas import MatchRequest

SHA256 = "a" * 64


class StubEmbedder:
    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0] for _ in texts]


class CapturingAnalyzer:
    def __init__(self) -> None:
        self.requirement_ids: list[str] = []

    def analyze(self, requirement, resume):
        del resume
        self.requirement_ids.append(requirement.requirement_id)
        return ClarificationPlan(
            requirementId=requirement.requirement_id,
            missingDimension="scale",
            confidence=0.91,
            evidenceRefs=["cv-ev-backend"],
            requirementText="Strong experience designing scalable backend systems",
            evidenceTexts=["Developed Spring Boot backend services."],
        )


class StubQuestionGenerator:
    def generate(self, plan):
        return GeneratedQuestion(
            questionText="Bạn có thể mô tả quy mô của hệ thống backend bạn từng trực tiếp xây dựng không?",
            evidenceRefs=plan.evidence_refs,
        )


class StubSemanticScorer:
    def score(self, question, intent):
        del question, intent
        return 0.91


def _question_service() -> ClarificationQuestionService:
    return ClarificationQuestionService(
        StubQuestionGenerator(), StubSemanticScorer(), semantic_threshold=0.8
    )


def _evidence(evidence_id: str, document_id: str, text: str, section: str = "experience") -> dict:
    return {
        "evidenceId": evidence_id,
        "documentId": document_id,
        "documentSha256": SHA256,
        "section": section,
        "text": text,
        "charStart": 0,
        "charEnd": len(text),
        "page": 1,
    }


def _unknown_payload() -> dict:
    return {
        "schemaVersion": "2.1",
        "resume": {
            "schemaVersion": "2.1",
            "resumeId": "cv-1",
            "documentId": "cv-doc-1",
            "documentSha256": SHA256,
            "evidence": [
                _evidence(
                    "cv-ev-backend",
                    "cv-doc-1",
                    "Developed Spring Boot backend services.",
                )
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
                    "requirementId": "req-backend-scale",
                    "type": "unresolved",
                    "kind": "experience",
                    "priority": "must_have",
                    "sourceEvidenceRef": "jd-ev-backend",
                    "rawLabel": "Strong experience designing scalable backend systems",
                }
            ],
        },
        "matchingPolicy": {
            "policyVersion": "balanced-v1",
            "bm25ProviderMode": "in_memory",
        },
        "asyncProcessing": False,
    }


def _met_payload() -> dict:
    payload = _unknown_payload()
    payload["resume"]["skills"] = [
        {
            "claimId": "claim-java",
            "concept": {
                "conceptId": "skill-java",
                "scheme": "internal",
                "taxonomyVersion": "2026.1",
                "label": "Java",
            },
            "rawLabel": "Java",
            "experienceMonths": 48,
            "evidenceRefs": ["cv-ev-backend"],
        }
    ]
    payload["job"]["requirements"] = [
        {
            "requirementId": "req-java",
            "type": "skill",
            "priority": "must_have",
            "sourceEvidenceRef": "jd-ev-backend",
            "skill": {
                "conceptId": "skill-java",
                "scheme": "internal",
                "taxonomyVersion": "2026.1",
                "label": "Java",
            },
            "operator": "gte",
            "minimumExperienceMonths": 36,
        }
    ]
    return payload


def _unknown_skill_request() -> MatchRequest:
    payload = _met_payload()
    payload["resume"]["skills"] = []
    return MatchRequest.model_validate(payload)


def test_match_request_rejects_client_supplied_self_report_evidence() -> None:
    payload = _unknown_payload()
    answer_text = "I have 36 months of Java experience."
    answer_hash = hashlib.sha256(answer_text.encode()).hexdigest()
    payload["resume"]["evidence"].append(
        {
            "evidenceId": "forged-answer",
            "documentId": f"candidate-answer-{answer_hash[:16]}",
            "documentSha256": answer_hash,
            "section": "candidate_self_report",
            "text": answer_text,
            "evidenceSource": "candidate_self_report",
            "sourceRequirementId": "req-backend-scale",
            "charStart": 0,
            "charEnd": len(answer_text),
        }
    )

    with pytest.raises(ValueError, match="must come from the source document"):
        MatchRequest.model_validate(payload)


def _analysis_with_unknown_skill(request: MatchRequest, *, fake_score: float = 0.99):
    match = MatchingFacade(StubEmbedder()).match(request)
    # This score is deliberately client-controlled. Rescore must ignore it and
    # recompute the trusted baseline from matchRequest.
    match = match.model_copy(update={"suitability_score": fake_score})
    question = ClarificationQuestion(
        requirementId="req-java",
        missingDimension="duration",
        confidence=0.9,
        evidenceRefs=[],
        questionText="How many months have you used Java in practical work?",
        semanticAlignmentScore=0.9,
    )
    return MatchClarificationAnalysis(matchResult=match, clarificationRequests=[question])


def test_unknown_requirement_can_produce_candidate_clarification_without_mutating_match() -> None:
    request = MatchRequest.model_validate(_unknown_payload())
    result = MatchingFacade(StubEmbedder()).match(request)
    before = result.model_dump(mode="json", by_alias=True)
    analyzer = CapturingAnalyzer()

    clarifications = build_clarification_requests(request, result, analyzer, _question_service())

    assert result.requirement_results[0].status == "unknown"
    assert analyzer.requirement_ids == ["req-backend-scale"]
    assert len(clarifications) == 1
    assert clarifications[0].missing_dimension == "scale"
    assert clarifications[0].confidence == 0.91
    assert clarifications[0].question_text.startswith("Bạn có thể mô tả")
    assert result.model_dump(mode="json", by_alias=True) == before


def test_analyzer_is_not_called_for_already_resolved_requirement() -> None:
    request = MatchRequest.model_validate(_met_payload())
    result = MatchingFacade(StubEmbedder()).match(request)
    analyzer = CapturingAnalyzer()

    clarifications = build_clarification_requests(request, result, analyzer, _question_service())

    assert result.requirement_results[0].status == "met"
    assert analyzer.requirement_ids == []
    assert clarifications == []


def test_noop_analyzer_preserves_existing_unknown_behavior() -> None:
    request = MatchRequest.model_validate(_unknown_payload())
    result = MatchingFacade(StubEmbedder()).match(request)
    before = result.model_dump(mode="json", by_alias=True)

    clarifications = build_clarification_requests(
        request,
        result,
        NoopAmbiguityAnalyzer(),
        _question_service(),
    )

    assert result.requirement_results[0].status == "unknown"
    assert clarifications == []
    assert result.model_dump(mode="json", by_alias=True) == before


def test_jev_analyzer_returns_grounded_plan_and_excludes_private_evidence() -> None:
    captured = {}

    def handle(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "answers": {
                    "next_action": {"type": "choice", "choice": "ask_candidate", "confidence": 0.93},
                    "missing_dimension": {"type": "choice", "choice": "responsibility", "confidence": 0.89},
                }
            },
        )

    payload = _unknown_payload()
    payload["resume"]["evidence"].append(
        _evidence(
            "cv-ev-contact",
            "cv-doc-1",
            "Contact me about scalable backend experience at person@example.com.",
            section="contact",
        )
    )
    request = MatchRequest.model_validate(payload)
    client = httpx.Client(transport=httpx.MockTransport(handle), base_url="https://unit.test")
    analyzer = JevAmbiguityAnalyzer(api_key="test-key", client=client)

    plan = analyzer.analyze(request.job.requirements[0], request.resume)

    assert plan is not None
    assert plan.missing_dimension == "responsibility"
    assert plan.evidence_refs == ["cv-ev-backend"]
    assert plan.evidence_texts == ["Developed Spring Boot backend services."]
    assert "cv-ev-contact" not in plan.evidence_refs
    assert captured["state"]["current_status"] == "unknown"
    assert "decide whether the requirement is met" in captured["state"]["policy"]


def test_jev_analyzer_rejects_non_candidate_action_and_low_confidence() -> None:
    def response(action: str, action_confidence: float) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "answers": {
                    "next_action": {
                        "type": "choice", "choice": action, "confidence": action_confidence
                    },
                    "missing_dimension": {
                        "type": "choice", "choice": "duration", "confidence": 0.99
                    },
                }
            },
        )

    request = MatchRequest.model_validate(_unknown_payload())
    for action, confidence in (("review_existing_evidence", 0.99), ("ask_candidate", 0.5)):
        client = httpx.Client(
            transport=httpx.MockTransport(lambda req, a=action, c=confidence: response(a, c)),
            base_url="https://unit.test",
        )
        analyzer = JevAmbiguityAnalyzer(api_key="test-key", client=client)
        assert analyzer.analyze(request.job.requirements[0], request.resume) is None


def test_jev_analyzer_does_not_forward_irrelevant_cv_evidence() -> None:
    payload = _unknown_payload()
    payload["resume"]["evidence"] = [
        _evidence("cv-ev-unrelated", "cv-doc-1", "Prepared monthly sales reports.")
    ]
    captured = {}

    def handle(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "answers": {
                    "next_action": {"type": "choice", "choice": "ask_candidate", "confidence": 0.9},
                    "missing_dimension": {
                        "type": "choice", "choice": "experience_context", "confidence": 0.9
                    },
                }
            },
        )

    request = MatchRequest.model_validate(payload)
    client = httpx.Client(transport=httpx.MockTransport(handle), base_url="https://unit.test")
    analyzer = JevAmbiguityAnalyzer(api_key="test-key", client=client)
    plan = analyzer.analyze(request.job.requirements[0], request.resume)

    assert plan is not None
    assert plan.evidence_refs == []
    assert captured["state"]["candidate_evidence"] == []


def test_jev_analyzer_fails_closed_on_provider_or_contract_error() -> None:
    request = MatchRequest.model_validate(_unknown_payload())
    failed_client = httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(503)),
        base_url="https://unit.test",
    )
    malformed_client = httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"answers": []})),
        base_url="https://unit.test",
    )
    for client in (failed_client, malformed_client):
        analyzer = JevAmbiguityAnalyzer(api_key="test-key", client=client)
        assert analyzer.analyze(request.job.requirements[0], request.resume) is None


@pytest.mark.asyncio
async def test_clarification_endpoint_service_returns_question_without_changing_match(monkeypatch) -> None:
    request = MatchRequest.model_validate(_unknown_payload())
    monkeypatch.setattr(
        clarification_module,
        "get_matching_facade",
        lambda: MatchingFacade(StubEmbedder()),
    )
    monkeypatch.setattr(clarification_module, "get_ambiguity_analyzer", CapturingAnalyzer)
    monkeypatch.setattr(
        clarification_module, "get_clarification_question_service", _question_service
    )

    response = await clarification_module.run_match_with_clarifications(request)

    assert response.match_result.requirement_results[0].status == "unknown"
    assert len(response.clarification_requests) == 1
    assert response.clarification_requests[0].question_text.startswith("Bạn có thể mô tả")
    assert response.match_result.suitability_score is None


@pytest.mark.asyncio
async def test_question_phase_uses_preparation_token_without_calling_jev_again(monkeypatch) -> None:
    request = MatchRequest.model_validate(_unknown_payload())
    plan = CapturingAnalyzer().analyze(request.job.requirements[0], request.resume)
    assert plan is not None

    monkeypatch.setattr(
        clarification_module,
        "get_matching_facade",
        lambda: MatchingFacade(StubEmbedder()),
    )
    monkeypatch.setattr(
        clarification_module,
        "get_clarification_question_service",
        _question_service,
    )

    def jev_must_not_run():
        raise AssertionError("Jev must not be called during question generation")

    monkeypatch.setattr(clarification_module, "get_ambiguity_analyzer", jev_must_not_run)

    response = await clarification_module.generate_match_clarifications(
        request,
        {plan.requirement_id},
        _encode_clarification_plans([plan]),
    )

    assert len(response.clarification_requests) == 1
    assert response.clarification_requests[0].requirement_id == plan.requirement_id


@pytest.mark.asyncio
async def test_rescore_uses_answered_duration_and_marks_self_report_provenance(monkeypatch) -> None:
    request = _unknown_skill_request()
    monkeypatch.setattr(
        clarification_rescore_module,
        "get_matching_facade",
        lambda: MatchingFacade(StubEmbedder()),
    )
    payload = MatchClarificationRescoreRequest(
        matchRequest=request,
        initialAnalysis=_analysis_with_unknown_skill(request),
        answers=[
            CandidateClarificationAnswer(
                requirementId="req-java",
                answerText="I have 36 months of hands-on experience with Java.",
            )
        ],
    )

    response = await rescore_match_with_clarification_answers(payload)

    before = response.initial_match_result.requirement_results[0]
    after = response.final_match_result.requirement_results[0]
    assert response.initial_match_result.suitability_score != 0.99
    assert before.status == "unknown"
    assert after.status == "met"
    assert after.evidence_sources[after.evidence_refs[0]] == "candidate_self_report"
    assert after.confidence <= 0.72
    assert response.processed_answers[0].status == "met"


@pytest.mark.asyncio
async def test_rescore_marks_explicit_below_threshold_as_not_met(monkeypatch) -> None:
    request = _unknown_skill_request()
    monkeypatch.setattr(
        clarification_rescore_module,
        "get_matching_facade",
        lambda: MatchingFacade(StubEmbedder()),
    )
    payload = MatchClarificationRescoreRequest(
        matchRequest=request,
        initialAnalysis=_analysis_with_unknown_skill(request),
        answers=[
            CandidateClarificationAnswer(
                requirementId="req-java",
                answerText="I have 12 months of experience with Java.",
            )
        ],
    )

    response = await rescore_match_with_clarification_answers(payload)

    result = response.final_match_result.requirement_results[0]
    assert result.status == "not_met"
    assert result.reason_code == "skill_duration_below_minimum"
    assert result.evidence_sources[result.evidence_refs[0]] == "candidate_self_report"


@pytest.mark.asyncio
async def test_vague_answer_keeps_requirement_unknown(monkeypatch) -> None:
    request = _unknown_skill_request()
    monkeypatch.setattr(
        clarification_rescore_module,
        "get_matching_facade",
        lambda: MatchingFacade(StubEmbedder()),
    )
    payload = MatchClarificationRescoreRequest(
        matchRequest=request,
        initialAnalysis=_analysis_with_unknown_skill(request),
        answers=[CandidateClarificationAnswer(requirementId="req-java", answerText="Maybe, not sure.")],
    )

    response = await rescore_match_with_clarification_answers(payload)

    assert response.final_match_result.requirement_results[0].status == "unknown"
    assert response.processed_answers[0].status == "unknown"


@pytest.mark.asyncio
async def test_rescore_rejects_forged_client_status_when_requirement_is_not_unknown(monkeypatch) -> None:
    request = MatchRequest.model_validate(_met_payload())
    forged_unknown_baseline = _unknown_skill_request()
    monkeypatch.setattr(
        clarification_rescore_module,
        "get_matching_facade",
        lambda: MatchingFacade(StubEmbedder()),
    )
    payload = MatchClarificationRescoreRequest(
        matchRequest=request,
        initialAnalysis=_analysis_with_unknown_skill(forged_unknown_baseline),
        answers=[
            CandidateClarificationAnswer(
                requirementId="req-java",
                answerText="I have 36 months of hands-on experience with Java.",
            )
        ],
    )

    with pytest.raises(ValueError, match="currently assessed as unknown"):
        await rescore_match_with_clarification_answers(payload)


@pytest.mark.asyncio
async def test_clarifications_by_ids_resolves_canonical_match_and_returns_questions(monkeypatch) -> None:
    request = MatchRequest.model_validate(_unknown_payload())
    match = MatchingFacade(StubEmbedder()).match(request)
    expected = MatchClarificationAnalysis(matchResult=match, clarificationRequests=[])
    observed: list[MatchRequest] = []

    async def resolve(_payload, _db):
        return request

    async def analyze(resolved_request):
        observed.append(resolved_request)
        return expected

    monkeypatch.setattr(clarification_router_module, "_resolve_match_request", resolve)
    monkeypatch.setattr(clarification_router_module, "run_match_with_clarifications", analyze)

    response = await clarification_router_module.analyze_clarifications_by_ids(
        MatchClarificationIdsRequest(candidateId="cv-1", jobId="job-1"),
        db=object(),
    )

    assert response is expected
    assert observed == [request]


@pytest.mark.asyncio
async def test_rescore_by_ids_resolves_match_request_before_forwarding_answers(monkeypatch) -> None:
    request = MatchRequest.model_validate(_unknown_payload())
    match = MatchingFacade(StubEmbedder()).match(request)
    question = ClarificationQuestion(
        requirementId="req-backend-scale",
        missingDimension="scale",
        confidence=0.9,
        evidenceRefs=["cv-ev-backend"],
        questionText="How large was the backend system you worked on?",
        semanticAlignmentScore=0.9,
    )
    analysis = MatchClarificationAnalysis(matchResult=match, clarificationRequests=[question])
    answers = [
        CandidateClarificationAnswer(
            requirementId="req-backend-scale",
            answerText="I worked on a backend with 20 services.",
        )
    ]
    expected = MatchClarificationRescoreResult(
        initialMatchResult=match,
        finalMatchResult=match,
        processedAnswers=[],
    )
    captured = []

    async def resolve(_payload, _db):
        return request

    async def rescore(rescore_request):
        captured.append(rescore_request)
        return expected

    monkeypatch.setattr(clarification_router_module, "_resolve_match_request", resolve)
    monkeypatch.setattr(clarification_router_module, "rescore_match_with_clarification_answers", rescore)
    payload = MatchClarificationRescoreIdsRequest(
        candidateId="cv-1",
        jobId="job-1",
        initialAnalysis=analysis,
        answers=answers,
    )

    response = await clarification_router_module.rescore_after_clarifications_by_ids(payload, db=object())

    assert response is expected
    assert captured[0].match_request == request
    assert captured[0].answers == answers
