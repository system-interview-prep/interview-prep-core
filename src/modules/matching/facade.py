from functools import lru_cache
from typing import Any

from src.modules.matching.schemas import (
    CanonicalResume,
    LanguageRequirement,
    RequirementResult,
    SkillRequirement,
    StructuredMatchRequest,
    StructuredMatchResult,
)
from src.modules.matching.rag.embedding import EmbeddingAdapter
from src.modules.matching.semantic import SemanticMatcher


class MatchingFacade:
    """Only supported entry point into matching from other business modules."""

    def __init__(self, embedder: EmbeddingAdapter | None = None) -> None:
        # Canonical rule matching must not require an embedding API key.  Delay
        # provider construction until the legacy text-similarity path is used.
        self._embedder = embedder
        self._matcher: SemanticMatcher | None = None

    def match(
        self,
        *,
        resume_text: str,
        job_description: str,
        algorithms: list[str],
        position: str | None = None,
        job_description_id: str | None = None,
        cv_id: str | None = None,
    ) -> dict[str, Any]:
        # ``algorithms`` remains in the API temporarily for old clients, but the
        # matching strategy is now always external embedding + cosine similarity.
        del algorithms
        if self._matcher is None:
            self._matcher = SemanticMatcher(self._embedder)
        return self._matcher.match(
            resume_text=resume_text,
            job_description=job_description,
            position=position,
            job_description_id=job_description_id,
            cv_id=cv_id,
        )

    def match_structured(self, payload: StructuredMatchRequest) -> StructuredMatchResult:
        """Match only evidence-backed canonical claims; never infer a missing claim."""
        skill_claims = {claim.concept.concept_id: claim for claim in payload.resume.skills}
        languages = {language.code: language for language in payload.resume.languages}
        results: list[RequirementResult] = []

        for requirement in payload.job.requirements:
            if isinstance(requirement, SkillRequirement):
                claim = skill_claims.get(requirement.skill.concept_id)
                if claim is None:
                    results.append(
                        RequirementResult(
                            requirementId=requirement.requirement_id,
                            status="unknown",
                            reasonCode="skill_not_evidenced",
                        )
                    )
                elif requirement.operator == "gte" and claim.experience_months is None:
                    results.append(
                        RequirementResult(
                            requirementId=requirement.requirement_id,
                            status="unknown",
                            evidenceRefs=claim.evidence_refs,
                            reasonCode="skill_duration_not_evidenced",
                        )
                    )
                elif (
                    requirement.operator == "gte"
                    and claim.experience_months < requirement.minimum_experience_months  # type: ignore[operator]
                ):
                    results.append(
                        RequirementResult(
                            requirementId=requirement.requirement_id,
                            status="not_met",
                            score=0.0,
                            evidenceRefs=claim.evidence_refs,
                            reasonCode="skill_duration_below_minimum",
                        )
                    )
                else:
                    results.append(
                        RequirementResult(
                            requirementId=requirement.requirement_id,
                            status="met",
                            score=1.0,
                            evidenceRefs=claim.evidence_refs,
                            reasonCode="skill_evidenced",
                        )
                    )
            elif isinstance(requirement, LanguageRequirement):
                claim = languages.get(requirement.language_code)
                if claim is None or (requirement.operator == "equal" and claim.level is None):
                    results.append(
                        RequirementResult(
                            requirementId=requirement.requirement_id,
                            status="unknown",
                            evidenceRefs=[] if claim is None else claim.evidence_refs,
                            reasonCode="language_level_not_evidenced",
                        )
                    )
                elif requirement.operator == "equal" and claim.level != requirement.minimum_level:
                    results.append(
                        RequirementResult(
                            requirementId=requirement.requirement_id,
                            status="not_met",
                            score=0.0,
                            evidenceRefs=claim.evidence_refs,
                            reasonCode="language_level_not_equal",
                        )
                    )
                else:
                    results.append(
                        RequirementResult(
                            requirementId=requirement.requirement_id,
                            status="met",
                            score=1.0,
                            evidenceRefs=claim.evidence_refs,
                            reasonCode="language_evidenced",
                        )
                    )

        return _structured_result(payload.resume, payload, results)


def _structured_result(
    resume: CanonicalResume, payload: StructuredMatchRequest, results: list[RequirementResult]
) -> StructuredMatchResult:
    scored = [result.score for result in results if result.score is not None]
    overall_score = sum(scored) / len(scored) if scored else 0.0
    must_have_failure = any(
        result.status == "not_met" and requirement.priority == "must_have"
        for result, requirement in zip(results, payload.job.requirements, strict=True)
    )
    must_have_unknown = any(
        result.status == "unknown" and requirement.priority == "must_have"
        for result, requirement in zip(results, payload.job.requirements, strict=True)
    )
    if must_have_failure and payload.matching_policy.must_have_mode == "strict":
        recommendation = "not_match"
    elif must_have_unknown and payload.matching_policy.unknown_handling == "manual_review":
        recommendation = "review"
    elif overall_score >= 0.8:
        recommendation = "strong_match"
    else:
        recommendation = "review"
    return StructuredMatchResult(
        schemaVersion="2.1",
        resumeId=resume.resume_id,
        jobId=payload.job.job_id,
        policyVersion=payload.matching_policy.policy_version,
        overallScore=overall_score,
        recommendation=recommendation,
        requirementResults=results,
    )


@lru_cache
def get_matching_facade() -> MatchingFacade:
    return MatchingFacade()
