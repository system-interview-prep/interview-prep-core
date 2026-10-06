"""Transport schemas for candidate clarification and rescore."""

from __future__ import annotations

from pydantic import Field, model_validator

from src.modules.matching.clarifications.question_generation import ClarificationQuestion
from src.modules.matching.domain.schemas import (
    CandidatePreferences,
    MatchingPolicy,
    MatchRequest,
    MatchResult,
)
from src.modules.user_cvs.domain.schemas import CanonicalModel


class MatchClarificationAnalysis(CanonicalModel):
    """Original match plus optional requests for additional factual evidence."""

    match_result: MatchResult
    clarification_requests: list[ClarificationQuestion] = Field(default_factory=list)


class CandidateClarificationAnswer(CanonicalModel):
    requirement_id: str = Field(min_length=1)
    answer_text: str = Field(min_length=1, max_length=5000)

    @model_validator(mode="after")
    def trim_and_reject_blank(self) -> CandidateClarificationAnswer:
        self.answer_text = self.answer_text.strip()
        if not self.answer_text:
            raise ValueError("answerText cannot be blank")
        return self


class MatchClarificationRescoreRequest(CanonicalModel):
    match_request: MatchRequest
    initial_analysis: MatchClarificationAnalysis
    answers: list[CandidateClarificationAnswer] = Field(min_length=1, max_length=30)

    @model_validator(mode="after")
    def answers_match_unknown_clarifications(self) -> MatchClarificationRescoreRequest:
        original = self.initial_analysis.match_result
        if original.resume_id != self.match_request.resume.resume_id:
            raise ValueError("initial match resumeId does not match matchRequest")
        if original.job_id != self.match_request.job.job_id:
            raise ValueError("initial match jobId does not match matchRequest")
        unknown_ids = {
            item.requirement_id
            for item in original.requirement_results
            if item.status == "unknown"
        }
        question_ids = [item.requirement_id for item in self.initial_analysis.clarification_requests]
        answer_ids = [item.requirement_id for item in self.answers]
        if len(question_ids) != len(set(question_ids)):
            raise ValueError("clarification questions must have unique requirementIds")
        if not set(question_ids).issubset(unknown_ids):
            raise ValueError("clarification questions may target only unknown requirements")
        if len(answer_ids) != len(set(answer_ids)):
            raise ValueError("answers must have unique requirementIds")
        if not set(answer_ids).issubset(set(question_ids)):
            raise ValueError("each answer must target a returned clarification question")
        return self


class MatchClarificationIdsRequest(CanonicalModel):
    """Resolve the canonical CV/JD server-side for browser callers."""

    candidate_id: str = Field(min_length=1)
    job_id: str = Field(min_length=1)
    matching_policy: MatchingPolicy = Field(default_factory=MatchingPolicy)
    candidate_preferences: CandidatePreferences = Field(default_factory=CandidatePreferences)


class MatchClarificationRescoreIdsRequest(MatchClarificationIdsRequest):
    initial_analysis: MatchClarificationAnalysis
    answers: list[CandidateClarificationAnswer] = Field(min_length=1, max_length=30)


class ClarificationAnswerOutcome(CanonicalModel):
    requirement_id: str
    evidence_ref: str
    evidence_source: str = "candidate_self_report"
    status: str


class MatchClarificationRescoreResult(CanonicalModel):
    initial_match_result: MatchResult
    final_match_result: MatchResult
    processed_answers: list[ClarificationAnswerOutcome]


