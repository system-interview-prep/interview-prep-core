"""The gate only spends an LLM call when the answer could change the decision."""

from src.modules.matching.clarifications.ambiguity import build_clarification_plans
from src.modules.matching.clarifications.question_generation import ClarificationPlan
from src.modules.matching.domain.schemas import MatchRequest, MatchResult

SHA256 = "a" * 64


class RecordingAnalyzer:
    """Approves every requirement it is handed, and remembers the order."""

    def __init__(self):
        self.seen: list[str] = []

    def analyze(self, requirement, resume):
        del resume
        self.seen.append(requirement.requirement_id)
        return ClarificationPlan(
            requirementId=requirement.requirement_id,
            missingDimension="duration",
            confidence=0.9,
            evidenceRefs=[],
            requirementText="text",
            subjectTerms=[],
            evidenceTexts=[],
        )


def _requirement(requirement_id: str, priority: str) -> dict:
    return {
        "requirementId": requirement_id,
        "type": "unresolved",
        "kind": "other",
        "priority": priority,
        "sourceEvidenceRef": "jd-ev-1",
        "rawLabel": f"requirement {requirement_id}",
        "atomicConcepts": [],
    }


def _request(*priorities: str) -> MatchRequest:
    return MatchRequest.model_validate(
        {
            "schemaVersion": "2.1",
            "resume": {
                "schemaVersion": "2.1",
                "resumeId": "cv-1",
                "documentId": "cv-doc-1",
                "documentSha256": SHA256,
                "rawText": "CV text",
                "skills": [],
                "evidence": [
                    {
                        "evidenceId": "cv-ev-1",
                        "documentId": "cv-doc-1",
                        "documentSha256": SHA256,
                        "section": "experience",
                        "text": "Backend work on a payment service",
                        "charStart": 0,
                        "charEnd": 33,
                    }
                ],
            },
            "job": {
                "schemaVersion": "2.1",
                "jobId": "job-1",
                "documentId": "job-doc-1",
                "documentSha256": SHA256,
                "jobTitle": "Backend Developer",
                "evidence": [
                    {
                        "evidenceId": "jd-ev-1",
                        "documentId": "job-doc-1",
                        "documentSha256": SHA256,
                        "section": "requirements",
                        "text": "Requirements",
                        "charStart": 0,
                        "charEnd": 12,
                    }
                ],
                "requirements": [
                    _requirement(f"req-{index}", priority)
                    for index, priority in enumerate(priorities)
                ],
            },
            "matchingPolicy": {"policyVersion": "balanced-v1", "bm25ProviderMode": "in_memory"},
        }
    )


def _result(request: MatchRequest, *, failed_must_have: list[str] | None = None) -> MatchResult:
    """Every requirement left ``unknown``, with the decision stated explicitly."""

    failed = failed_must_have or []
    return MatchResult.model_validate(
        {
            "schemaVersion": "2.1",
            "resumeId": request.resume.resume_id,
            "jobId": request.job.job_id,
            "policyVersion": "balanced-v1",
            "eligibility": "ineligible" if failed else "review_required",
            "fitBand": "not_eligible" if failed else "review_required",
            "decision": "assessed" if failed else "abstained",
            "failedMustHaveRequirements": failed,
            "requirementResults": [
                {
                    "requirementId": requirement.requirement_id,
                    "status": "unknown",
                    "score": 0.0,
                    "confidence": 0.35,
                    "evidenceRefs": ["cv-ev-1"],
                    "reasonCode": "generic_requirement_evidence_weak",
                }
                for requirement in request.job.requirements
            ],
            "factorResults": [],
        }
    )


class TestThresholdSelectionIsUnchanged:
    def test_every_unknown_still_reaches_the_gate(self):
        request = _request("must_have", "nice_to_have")
        analyzer = RecordingAnalyzer()
        plans = build_clarification_plans(request, _result(request), analyzer)
        assert analyzer.seen == ["req-0", "req-1"]
        assert len(plans) == 2

    def test_a_disqualified_pair_is_still_analysed(self):
        """The current behaviour: the threshold cannot see the decision."""
        request = _request("must_have", "nice_to_have")
        analyzer = RecordingAnalyzer()
        build_clarification_plans(request, _result(request, failed_must_have=["req-x"]), analyzer)
        assert analyzer.seen == ["req-0", "req-1"]


class TestInformationGainSelection:
    def test_a_disqualified_pair_costs_no_llm_call(self):
        request = _request("must_have", "nice_to_have")
        analyzer = RecordingAnalyzer()
        plans = build_clarification_plans(
            request,
            _result(request, failed_must_have=["req-x"]),
            analyzer,
            None,
            "information_gain",
            0,
        )
        assert analyzer.seen == []
        assert plans == []

    def test_only_requirements_that_can_change_the_decision_are_asked(self):
        request = _request("must_have", "nice_to_have", "context")
        analyzer = RecordingAnalyzer()
        build_clarification_plans(
            request, _result(request), analyzer, None, "information_gain", 0
        )
        assert analyzer.seen == ["req-0"]

    def test_the_question_budget_caps_the_calls(self):
        request = _request("must_have", "must_have", "must_have")
        analyzer = RecordingAnalyzer()
        build_clarification_plans(
            request, _result(request), analyzer, None, "information_gain", 2
        )
        assert len(analyzer.seen) == 2

    def test_selection_is_restricted_to_the_requested_ids(self):
        request = _request("must_have", "must_have")
        analyzer = RecordingAnalyzer()
        build_clarification_plans(
            request, _result(request), analyzer, {"req-1"}, "information_gain", 0
        )
        assert analyzer.seen == ["req-1"]
