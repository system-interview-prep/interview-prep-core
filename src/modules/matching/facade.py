from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from functools import lru_cache

from src.modules.matching.rag.embedding import EmbeddingAdapter, build_embedding_adapter_from_env
from src.modules.matching.schemas import (
    CanonicalJob,
    CompatibilityResult,
    FactorResult,
    LanguageRequirement,
    MatchRequest,
    MatchResult,
    Requirement,
    RequirementResult,
    SkillRequirement,
    UnresolvedRequirement,
)
from src.modules.matching.semantic import cosine_similarity
from src.modules.user_cvs.schemas import CanonicalResume

POLICY_WEIGHTS = {
    "balanced-v1": {"skill": 0.30, "experience": 0.30, "semantic": 0.30, "language": 0.10},
    "skill-focus-v1": {"skill": 0.40, "experience": 0.25, "semantic": 0.30, "language": 0.05},
    "experience-focus-v1": {"skill": 0.25, "experience": 0.40, "semantic": 0.30, "language": 0.05},
}

PROFICIENCY_RANK = {
    "basic": 1,
    "beginner": 1,
    "competent": 2,
    "intermediate": 2,
    "advanced": 3,
    "expert": 3,
}


class MatchingFacade:
    """The only application entry point for one CV-to-one-JD fit assessment."""

    def __init__(self, embedder: EmbeddingAdapter | None = None) -> None:
        self._embedder = embedder

    def match(self, payload: MatchRequest) -> MatchResult:
        requirements = self._evaluate_requirements(payload.resume, payload.job)
        eligibility = self._eligibility(payload, requirements)
        compatibility_results = self._evaluate_compatibility(payload)
        compatibility_status = self._compatibility_status(compatibility_results)
        factors, warnings = self._score_factors(payload, requirements)
        self._apply_effective_weights(factors)
        suitability = self._suitability(factors)
        decision, fit_band = self._decision(eligibility, suitability, factors)
        return MatchResult(
            resumeId=payload.resume.resume_id,
            jobId=payload.job.job_id,
            policyVersion=payload.matching_policy.policy_version,
            eligibility=eligibility,
            compatibilityStatus=compatibility_status,
            suitabilityScore=suitability,
            fitBand=fit_band,
            decision=decision,
            requirementResults=requirements,
            compatibilityResults=compatibility_results,
            factorResults=factors,
            warnings=warnings,
        )

    @staticmethod
    def _evaluate_requirements(resume: CanonicalResume, job: CanonicalJob) -> list[RequirementResult]:
        skill_claims = {claim.concept.concept_id: claim for claim in resume.skills}
        languages = {language.code: language for language in resume.languages}
        results: list[RequirementResult] = []
        for requirement in job.requirements:
            if isinstance(requirement, SkillRequirement):
                claim = skill_claims.get(requirement.skill.concept_id)
                if claim is None:
                    results.append(
                        RequirementResult(
                            requirementId=requirement.requirement_id,
                            status="unknown",
                            confidence=0.0,
                            reasonCode="skill_not_evidenced",
                        )
                    )
                elif requirement.operator == "proficiency_gte" and claim.proficiency_level is None:
                    results.append(
                        RequirementResult(
                            requirementId=requirement.requirement_id,
                            status="unknown",
                            confidence=claim.confidence or 0.5,
                            evidenceRefs=claim.evidence_refs,
                            reasonCode="skill_level_not_evidenced",
                        )
                    )
                elif (
                    requirement.operator == "proficiency_gte"
                    and PROFICIENCY_RANK[claim.proficiency_level]  # type: ignore[index]
                    < PROFICIENCY_RANK[requirement.minimum_proficiency_level]  # type: ignore[index]
                ):
                    results.append(
                        RequirementResult(
                            requirementId=requirement.requirement_id,
                            status="not_met",
                            score=0.0,
                            confidence=claim.confidence or 1.0,
                            evidenceRefs=claim.evidence_refs,
                            reasonCode="skill_level_below_minimum",
                        )
                    )
                elif requirement.operator == "gte" and claim.experience_months is None:
                    results.append(
                        RequirementResult(
                            requirementId=requirement.requirement_id,
                            status="unknown",
                            confidence=claim.confidence or 0.5,
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
                            confidence=claim.confidence or 1.0,
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
                            confidence=claim.confidence or 1.0,
                            evidenceRefs=claim.evidence_refs,
                            reasonCode=(
                                "skill_and_level_evidenced"
                                if requirement.operator == "proficiency_gte"
                                else "skill_evidenced"
                            ),
                        )
                    )
            elif isinstance(requirement, LanguageRequirement):
                claim = languages.get(requirement.language_code)
                if claim is None or (requirement.operator == "equal" and claim.level is None):
                    results.append(
                        RequirementResult(
                            requirementId=requirement.requirement_id,
                            status="unknown",
                            confidence=0.0,
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
                            confidence=1.0,
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
                            confidence=1.0,
                            evidenceRefs=claim.evidence_refs,
                            reasonCode="language_evidenced",
                        )
                    )
            elif isinstance(requirement, UnresolvedRequirement):
                results.append(
                    RequirementResult(
                        requirementId=requirement.requirement_id,
                        status="unknown",
                        confidence=0.0,
                        reasonCode=f"{requirement.kind}_requirement_needs_specialized_evaluator",
                    )
                )
        return results

    @staticmethod
    def _eligibility(payload: MatchRequest, results: list[RequirementResult]) -> str:
        must = [
            result
            for requirement, result in zip(payload.job.requirements, results, strict=True)
            if requirement.priority == "must_have"
        ]
        if payload.matching_policy.must_have_mode == "strict" and any(
            result.status == "not_met" and result.confidence >= 0.80 for result in must
        ):
            return "ineligible"
        if payload.matching_policy.unknown_handling == "manual_review" and any(
            result.status == "unknown" for result in must
        ):
            return "review_required"
        return "eligible"

    def _score_factors(
        self, payload: MatchRequest, requirements: list[RequirementResult]
    ) -> tuple[list[FactorResult], list[str]]:
        weights = POLICY_WEIGHTS[payload.matching_policy.policy_version]
        pairs = list(zip(payload.job.requirements, requirements, strict=True))
        factors = [
            self._requirement_factor(
                "skill",
                weights["skill"],
                pairs,
                lambda requirement: (
                    isinstance(requirement, SkillRequirement) and requirement.priority != "must_have"
                ),
            ),
            self._experience_factor(payload, weights["experience"], pairs),
            self._requirement_factor(
                "language",
                weights["language"],
                pairs,
                lambda requirement: isinstance(requirement, LanguageRequirement),
            ),
        ]
        semantic, warning = self._semantic_factor(payload, weights["semantic"])
        factors.append(semantic)
        return factors, [warning] if warning else []

    def _experience_factor(
        self,
        payload: MatchRequest,
        policy_weight: float,
        pairs: list[tuple[Requirement, RequirementResult]],
    ) -> FactorResult:
        job = payload.job
        resume = payload.resume
        if not job.job_title and not job.career_classifications:
            return self._requirement_factor(
                "experience",
                policy_weight,
                pairs,
                lambda requirement: (
                    isinstance(requirement, SkillRequirement)
                    and requirement.operator == "gte"
                    and requirement.priority != "must_have"
                ),
            )

        title_scores = [
            self._token_overlap(job.job_title, employment.job_title)
            for employment in resume.employment
            if job.job_title
        ]
        job_domains = {item.code for item in job.career_classifications}
        resume_domains = {item.code for item in resume.career_classifications}
        domain_score = 1.0 if job_domains & resume_domains else 0.0
        components = []
        if title_scores:
            components.append(max(title_scores))
        if job_domains and resume_domains:
            components.append(domain_score)
        evidence = list(
            dict.fromkeys(
                ref
                for owner in [*resume.employment, *resume.career_classifications]
                for ref in owner.evidence_refs
            )
        )
        if not components:
            return FactorResult(
                factor="experience",
                status="unknown",
                reliability=0.0,
                policyWeight=policy_weight,
                effectiveWeight=0.0,
                warningCode="career_experience_not_evidenced",
            )
        return FactorResult(
            factor="experience",
            status="scored",
            rawScore=sum(components) / len(components),
            reliability=1.0 if evidence else 0.5,
            policyWeight=policy_weight,
            effectiveWeight=0.0,
            evidenceRefs=evidence,
            warningCode=None if evidence else "career_experience_has_no_direct_evidence",
        )

    @staticmethod
    def _requirement_factor(
        factor: str,
        policy_weight: float,
        pairs: list[tuple[Requirement, RequirementResult]],
        predicate: Callable[[Requirement], bool],
    ) -> FactorResult:
        selected = [(requirement, result) for requirement, result in pairs if predicate(requirement)]
        if not selected:
            return FactorResult(
                factor=factor,
                status="not_applicable",
                reliability=0.0,
                policyWeight=policy_weight,
                effectiveWeight=0.0,
            )
        scored = [result for _, result in selected if result.score is not None]
        evidence = [ref for _, result in selected for ref in result.evidence_refs]
        if not scored:
            return FactorResult(
                factor=factor,
                status="unknown",
                reliability=0.0,
                policyWeight=policy_weight,
                effectiveWeight=0.0,
                evidenceRefs=evidence,
                warningCode="factor_not_evidenced",
            )
        score = sum(result.score for result in scored if result.score is not None) / len(scored)
        reliability = sum(result.confidence for result in scored) / len(scored)
        return FactorResult(
            factor=factor,
            status="scored",
            rawScore=score,
            reliability=reliability,
            policyWeight=policy_weight,
            effectiveWeight=0.0,
            evidenceRefs=evidence,
        )

    def _semantic_factor(
        self, payload: MatchRequest, policy_weight: float
    ) -> tuple[FactorResult, str | None]:
        contextual_resume_text = [
            text
            for employment in payload.resume.employment
            for text in employment.responsibilities
        ]
        contextual_resume_text.extend(
            achievement.text
            for employment in payload.resume.employment
            for achievement in employment.achievements
        )
        contextual_resume_text.extend(
            project.description for project in payload.resume.projects if project.description
        )
        contextual_job_text = [item.text for item in payload.job.responsibilities]
        resume_text = "\n".join(contextual_resume_text) or "\n".join(
            item.text for item in payload.resume.evidence
        )
        job_text = "\n".join(contextual_job_text) or "\n".join(
            item.text for item in payload.job.evidence
        )
        if not resume_text or not job_text:
            return self._unknown_semantic(policy_weight, "semantic_input_not_evidenced")
        try:
            embedder = self._embedder or build_embedding_adapter_from_env()
            vectors = embedder.embed_texts([resume_text, job_text])
            if len(vectors) != 2:
                raise ValueError("embedding provider returned an invalid vector count")
            score = cosine_similarity(vectors[0], vectors[1])
        except Exception:
            return self._unknown_semantic(policy_weight, "semantic_scorer_unavailable")
        return (
            FactorResult(
                factor="semantic",
                status="scored",
                rawScore=score,
                reliability=1.0,
                policyWeight=policy_weight,
                effectiveWeight=0.0,
                evidenceRefs=[item.evidence_id for item in payload.resume.evidence],
            ),
            None,
        )

    @staticmethod
    def _token_overlap(left: str | None, right: str | None) -> float:
        def tokens(value: str | None) -> set[str]:
            normalized = unicodedata.normalize("NFKD", value or "")
            ascii_value = "".join(char for char in normalized if not unicodedata.combining(char))
            return set(re.findall(r"[a-z0-9+#.]+", ascii_value.casefold()))

        left_tokens, right_tokens = tokens(left), tokens(right)
        if not left_tokens or not right_tokens:
            return 0.0
        return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)

    @staticmethod
    def _evaluate_compatibility(payload: MatchRequest) -> list[CompatibilityResult]:
        preferences = payload.candidate_preferences
        job = payload.job
        results: list[CompatibilityResult] = []

        if job.work_mode is None:
            results.append(
                CompatibilityResult(
                    criterion="work_mode",
                    status="not_applicable",
                    confidence=1.0,
                    reasonCode="job_work_mode_not_specified",
                )
            )
        elif not preferences.accepted_work_modes:
            results.append(
                CompatibilityResult(
                    criterion="work_mode",
                    status="unknown",
                    confidence=0.0,
                    reasonCode="candidate_work_mode_preference_missing",
                )
            )
        else:
            compatible = job.work_mode in preferences.accepted_work_modes
            results.append(
                CompatibilityResult(
                    criterion="work_mode",
                    status="compatible" if compatible else "incompatible",
                    confidence=1.0,
                    reasonCode=(
                        "work_mode_accepted" if compatible else "work_mode_not_accepted"
                    ),
                )
            )

        if job.work_mode == "remote" or job.location is None:
            results.append(
                CompatibilityResult(
                    criterion="location",
                    status="not_applicable",
                    confidence=1.0,
                    reasonCode=(
                        "remote_job_has_no_location_constraint"
                        if job.work_mode == "remote"
                        else "job_location_not_specified"
                    ),
                )
            )
        elif not preferences.accepted_locations:
            results.append(
                CompatibilityResult(
                    criterion="location",
                    status="unknown",
                    confidence=0.0,
                    reasonCode="candidate_location_preference_missing",
                )
            )
        else:
            location_matches = any(
                MatchingFacade._token_overlap(job.location, location) == 1.0
                for location in preferences.accepted_locations
            )
            if location_matches or preferences.willing_to_relocate is True:
                results.append(
                    CompatibilityResult(
                        criterion="location",
                        status="compatible",
                        confidence=1.0,
                        reasonCode=(
                            "location_accepted" if location_matches else "candidate_willing_to_relocate"
                        ),
                    )
                )
            elif preferences.willing_to_relocate is False:
                results.append(
                    CompatibilityResult(
                        criterion="location",
                        status="incompatible",
                        confidence=1.0,
                        reasonCode="location_not_accepted_and_no_relocation",
                    )
                )
            else:
                results.append(
                    CompatibilityResult(
                        criterion="location",
                        status="unknown",
                        confidence=0.5,
                        reasonCode="relocation_preference_missing",
                    )
                )
        return results

    @staticmethod
    def _compatibility_status(results: list[CompatibilityResult]) -> str:
        statuses = {item.status for item in results}
        if "incompatible" in statuses:
            return "incompatible"
        if "unknown" in statuses:
            return "unknown"
        if "compatible" in statuses:
            return "compatible"
        return "not_applicable"

    @staticmethod
    def _unknown_semantic(policy_weight: float, warning: str) -> tuple[FactorResult, str]:
        return (
            FactorResult(
                factor="semantic",
                status="unknown",
                reliability=0.0,
                policyWeight=policy_weight,
                effectiveWeight=0.0,
                warningCode=warning,
            ),
            warning,
        )

    @staticmethod
    def _apply_effective_weights(factors: list[FactorResult]) -> None:
        denominator = sum(
            item.policy_weight * item.reliability for item in factors if item.status == "scored"
        )
        for item in factors:
            item.effective_weight = (
                item.policy_weight * item.reliability / denominator
                if denominator and item.status == "scored"
                else 0.0
            )

    @staticmethod
    def _suitability(factors: list[FactorResult]) -> float | None:
        scored = [item for item in factors if item.status == "scored" and item.raw_score is not None]
        if not scored:
            return None
        return sum(item.effective_weight * item.raw_score for item in scored if item.raw_score is not None)

    @staticmethod
    def _decision(
        eligibility: str, suitability: float | None, factors: list[FactorResult]
    ) -> tuple[str, str]:
        if eligibility == "ineligible":
            return "assessed", "not_eligible"
        if eligibility == "review_required":
            return "abstained", "review_required"
        reliability = sum(item.reliability for item in factors if item.status == "scored")
        if suitability is None or reliability < 0.60:
            return "abstained", "insufficient_evidence"
        return "assessed", "strong_fit" if suitability >= 0.80 else "partial_fit"


@lru_cache
def get_matching_facade() -> MatchingFacade:
    return MatchingFacade()
