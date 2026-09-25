from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from functools import lru_cache

from src.modules.matching.bm25 import bm25_similarity
from src.modules.matching.bm25_provider import Bm25Provider, get_bm25_provider
from src.modules.matching.rag.embedding import EmbeddingAdapter, build_embedding_adapter_from_env
from src.modules.matching.requirement_evaluators import (
    evaluate_unresolved_requirement,
    select_evaluator,
)
from src.modules.matching.schemas import (
    CanonicalJob,
    CompatibilityResult,
    FactorResult,
    LanguageRequirement,
    MatchRequest,
    MatchResult,
    Requirement,
    RequirementResult,
    ScoreProvenance,
    SkillRequirement,
    UnresolvedRequirement,
)
from src.modules.matching.semantic import cosine_similarity
from src.modules.matching.targeted_reparse import reparse_partial_resume
from src.modules.user_cvs.schemas import CanonicalResume

ROLE_SYNONYMS = {
    "engineer": "developer",
    "programmer": "developer",
    "coder": "developer",
    "dev": "developer",
    "architect": "lead",
    "tester": "qa",
    "qc": "qa",
    "admin": "administrator",
    "specialist": "expert",
}

POLICY_WEIGHTS = {
    # Grounded requirement satisfaction is the primary fit signal. Skill and
    # semantic factors add breadth and context, but are intentionally secondary
    # because they can overlap with requirement evidence.
    "balanced-v1": {
        "requirement_coverage": 0.60,
        "skill": 0.15,
        "experience": 0.05,
        "language": 0.05,
        "semantic": 0.15,
    },
    "skill-focus-v1": {
        "requirement_coverage": 0.50,
        "skill": 0.25,
        "experience": 0.05,
        "language": 0.05,
        "semantic": 0.15,
    },
    "experience-focus-v1": {
        "requirement_coverage": 0.50,
        "skill": 0.10,
        "experience": 0.20,
        "language": 0.05,
        "semantic": 0.15,
    },
}

_REQUIREMENT_IMPORTANCE = {"must_have": 1.0, "nice_to_have": 0.35, "context": 0.15}

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

    def __init__(
        self,
        embedder: EmbeddingAdapter | None = None,
        bm25_provider: Bm25Provider | None = None,
    ) -> None:
        self._embedder = embedder
        self._bm25_provider = bm25_provider or get_bm25_provider()

    def match(self, payload: MatchRequest) -> MatchResult:
        payload = payload.model_copy(
            update={"resume": reparse_partial_resume(payload.resume, payload.job)}
        )
        requirements = self._evaluate_requirements(payload.resume, payload.job)
        eligibility = self._eligibility(payload, requirements)
        compatibility_results = self._evaluate_compatibility(payload)
        compatibility_status = self._compatibility_status(compatibility_results)
        factors, warnings = self._score_factors(payload, requirements)
        self._apply_effective_weights(factors)
        diagnostic_score = self._suitability(factors)
        score_provenance = self._score_provenance(requirements, factors)
        semantic_only = score_provenance.mode == "semantic_only_estimated"
        if semantic_only:
            warnings.extend(["semantic_only_estimate", "semantic_only_score_suppressed"])
        suitability = diagnostic_score if eligibility == "eligible" and not semantic_only else None
        decision, fit_band = self._decision(eligibility, suitability, factors)
        return MatchResult(
            resumeId=payload.resume.resume_id,
            jobId=payload.job.job_id,
            jobVersionId=payload.job.job_version_id,
            policyVersion=payload.matching_policy.policy_version,
            eligibility=eligibility,
            compatibilityStatus=compatibility_status,
            diagnosticScore=diagnostic_score,
            suitabilityScore=suitability,
            fitBand=fit_band,
            decision=decision,
            failedMustHaveRequirements=self._failed_must_have_requirements(payload, requirements),
            requirementResults=requirements,
            compatibilityResults=compatibility_results,
            factorResults=factors,
            scoreProvenance=score_provenance,
            warnings=warnings,
        )

    async def match_async(self, payload: MatchRequest) -> MatchResult:
        """Assess a match while keeping database I/O on the caller's event loop."""
        payload = payload.model_copy(
            update={"resume": reparse_partial_resume(payload.resume, payload.job)}
        )
        requirements = self._evaluate_requirements(payload.resume, payload.job)
        eligibility = self._eligibility(payload, requirements)
        compatibility_results = self._evaluate_compatibility(payload)
        compatibility_status = self._compatibility_status(compatibility_results)
        factors, warnings = await self._score_factors_async(payload, requirements)
        self._apply_effective_weights(factors)
        diagnostic_score = self._suitability(factors)
        score_provenance = self._score_provenance(requirements, factors)
        semantic_only = score_provenance.mode == "semantic_only_estimated"
        if semantic_only:
            warnings.extend(["semantic_only_estimate", "semantic_only_score_suppressed"])
        suitability = diagnostic_score if eligibility == "eligible" and not semantic_only else None
        decision, fit_band = self._decision(eligibility, suitability, factors)
        return MatchResult(
            resumeId=payload.resume.resume_id,
            jobId=payload.job.job_id,
            jobVersionId=payload.job.job_version_id,
            policyVersion=payload.matching_policy.policy_version,
            eligibility=eligibility,
            compatibilityStatus=compatibility_status,
            diagnosticScore=diagnostic_score,
            suitabilityScore=suitability,
            fitBand=fit_band,
            decision=decision,
            failedMustHaveRequirements=self._failed_must_have_requirements(payload, requirements),
            requirementResults=requirements,
            compatibilityResults=compatibility_results,
            factorResults=factors,
            scoreProvenance=score_provenance,
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
                results.append(evaluate_unresolved_requirement(requirement, resume))
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

    @staticmethod
    def _failed_must_have_requirements(
        payload: MatchRequest, results: list[RequirementResult]
    ) -> list[str]:
        return [
            requirement.requirement_id
            for requirement, result in zip(payload.job.requirements, results, strict=True)
            if requirement.priority == "must_have" and result.status == "not_met"
        ]

    def _score_factors(
        self, payload: MatchRequest, requirements: list[RequirementResult]
    ) -> tuple[list[FactorResult], list[str]]:
        weights = POLICY_WEIGHTS[payload.matching_policy.policy_version]
        pairs = list(zip(payload.job.requirements, requirements, strict=True))
        factors = [
            self._requirement_coverage_factor(weights["requirement_coverage"], pairs),
            self._requirement_factor(
                "skill",
                weights["skill"],
                pairs,
                lambda requirement: self._requirement_category(requirement) == "skill",
            ),
            self._experience_factor(payload, weights["experience"], pairs),
            self._requirement_factor(
                "language",
                weights["language"],
                pairs,
                lambda requirement: self._requirement_category(requirement) == "language",
            ),
        ]
        semantic, warning = self._semantic_factor(payload, weights["semantic"])
        factors.append(semantic)
        return factors, [warning] if warning else []

    async def _score_factors_async(
        self, payload: MatchRequest, requirements: list[RequirementResult]
    ) -> tuple[list[FactorResult], list[str]]:
        weights = POLICY_WEIGHTS[payload.matching_policy.policy_version]
        pairs = list(zip(payload.job.requirements, requirements, strict=True))
        factors = [
            self._requirement_coverage_factor(weights["requirement_coverage"], pairs),
            self._requirement_factor(
                "skill", weights["skill"], pairs, lambda r: self._requirement_category(r) == "skill"
            ),
            self._experience_factor(payload, weights["experience"], pairs),
            self._requirement_factor(
                "language", weights["language"], pairs, lambda r: self._requirement_category(r) == "language"
            ),
        ]
        semantic, warning = await self._semantic_factor_async(payload, weights["semantic"])
        factors.append(semantic)
        return factors, [warning] if warning else []

    @staticmethod
    def _requirement_coverage_factor(
        policy_weight: float,
        pairs: list[tuple[Requirement, RequirementResult]],
    ) -> FactorResult:
        applicable = [
            (requirement, result)
            for requirement, result in pairs
            if result.status != "not_applicable" and result.reason_code != "requirement_evaluator_unsupported"
        ]
        if not applicable:
            return FactorResult(
                factor="requirement_coverage",
                status="not_applicable",
                reliability=0.0,
                policyWeight=policy_weight,
                effectiveWeight=0.0,
            )
        evaluated = [(requirement, result) for requirement, result in applicable if result.score is not None]
        evidence = list(dict.fromkeys(ref for _, result in applicable for ref in result.evidence_refs))
        applicable_weight = sum(
            _REQUIREMENT_IMPORTANCE[requirement.priority] for requirement, _ in applicable
        )
        evaluated_weight = sum(_REQUIREMENT_IMPORTANCE[requirement.priority] for requirement, _ in evaluated)
        if not evaluated:
            return FactorResult(
                factor="requirement_coverage",
                status="unknown",
                reliability=0.0,
                policyWeight=policy_weight,
                effectiveWeight=0.0,
                evidenceRefs=evidence,
                warningCode="requirements_not_evaluated",
            )
        score = (
            sum(
                _REQUIREMENT_IMPORTANCE[requirement.priority] * (result.score or 0.0)
                for requirement, result in evaluated
            )
            / evaluated_weight
        )
        return FactorResult(
            factor="requirement_coverage",
            status="scored",
            rawScore=score,
            # Unknown evidence is excluded from satisfaction, but lowers confidence
            # through reliability rather than being silently counted as a failure.
            reliability=evaluated_weight / applicable_weight,
            policyWeight=policy_weight,
            effectiveWeight=0.0,
            evidenceRefs=evidence,
        )

    def _experience_factor(
        self,
        payload: MatchRequest,
        policy_weight: float,
        pairs: list[tuple[Requirement, RequirementResult]],
    ) -> FactorResult:
        del payload
        return self._requirement_factor(
            "experience",
            policy_weight,
            pairs,
            lambda requirement: self._requirement_category(requirement) == "experience",
        )

    @staticmethod
    def _requirement_category(requirement: Requirement) -> str | None:
        if isinstance(requirement, SkillRequirement):
            return "skill"
        if isinstance(requirement, LanguageRequirement):
            return "language"
        if isinstance(requirement, UnresolvedRequirement):
            selection = select_evaluator(requirement)
            return selection.category if selection else None
        return None

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
        evidence = list(dict.fromkeys(ref for _, result in selected for ref in result.evidence_refs))
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
        self, payload: MatchRequest, policy_weight: float, sparse_score: float | None = None
    ) -> tuple[FactorResult, str | None]:
        contextual_resume_text = [
            value for value in (payload.resume.profile.headline, payload.resume.profile.summary) if value
        ]
        contextual_resume_text.extend(claim.concept.label for claim in payload.resume.skills)
        contextual_resume_text.extend(item.label for item in payload.resume.career_classifications)
        contextual_resume_text.extend(project.name for project in payload.resume.projects)
        contextual_resume_text.extend(
            text for employment in payload.resume.employment for text in employment.responsibilities
        )
        contextual_resume_text.extend(
            achievement.text
            for employment in payload.resume.employment
            for achievement in employment.achievements
        )
        contextual_resume_text.extend(
            project.description for project in payload.resume.projects if project.description
        )
        contextual_job_text = [value for value in (payload.job.job_title,) if value]
        contextual_job_text.extend(
            item.raw_label for item in payload.job.requirements if isinstance(item, UnresolvedRequirement)
        )
        contextual_job_text.extend(
            item.skill.label for item in payload.job.requirements if isinstance(item, SkillRequirement)
        )
        contextual_job_text.extend(item.text for item in payload.job.responsibilities)
        contextual_job_text.extend(item.label for item in payload.job.career_classifications)
        resume_text = "\n".join(contextual_resume_text) or "\n".join(
            item.text for item in payload.resume.evidence
        )
        job_text = "\n".join(contextual_job_text) or "\n".join(item.text for item in payload.job.evidence)
        if not resume_text or not job_text:
            return self._unknown_semantic(policy_weight, "semantic_input_not_evidenced")

        if sparse_score is None:
            if payload.matching_policy.bm25_provider_mode == "in_memory":
                sparse_score = bm25_similarity(job_text, resume_text)
            else:
                doc_id = payload.resume.document_id or payload.resume.resume_id
                sparse_score = self._bm25_provider.score(
                    query_text=job_text, doc_text=resume_text, doc_id=doc_id
                )
        mode = payload.matching_policy.semantic_mode
        bm25_w = payload.matching_policy.bm25_weight

        if mode == "sparse_only":
            return (
                FactorResult(
                    factor="semantic",
                    status="scored",
                    rawScore=sparse_score,
                    reliability=0.90,
                    policyWeight=policy_weight,
                    effectiveWeight=0.0,
                    evidenceRefs=[item.evidence_id for item in payload.resume.evidence],
                    sparseScore=sparse_score,
                ),
                None,
            )

        dense_score: float | None = None
        dense_disabled: bool = False
        try:
            # Reuse one provider client for the facade lifetime. Apart from reducing
            # connection setup overhead, this lets disk-cache hits stay local across
            # every pair in a batch evaluation.
            if self._embedder is None:
                self._embedder = build_embedding_adapter_from_env()
            embedder = self._embedder
            vectors = embedder.embed_texts([resume_text, job_text])
            if len(vectors) != 2:
                raise ValueError("embedding provider returned an invalid vector count")
            dense_score = cosine_similarity(vectors[0], vectors[1])
        except Exception as exc:
            dense_score = None
            if "disabled" in str(exc).lower():
                dense_disabled = True

        if dense_score is not None:
            if mode == "dense_only":
                final_score = dense_score
            else:  # hybrid
                final_score = (1.0 - bm25_w) * dense_score + bm25_w * sparse_score

            return (
                FactorResult(
                    factor="semantic",
                    status="scored",
                    rawScore=round(final_score, 4),
                    reliability=1.0,
                    policyWeight=policy_weight,
                    effectiveWeight=0.0,
                    evidenceRefs=[item.evidence_id for item in payload.resume.evidence],
                    denseScore=round(dense_score, 4),
                    sparseScore=round(sparse_score, 4),
                ),
                None,
            )

        # When semantic is explicitly disabled, preserve the disabled semantic contract
        if dense_disabled:
            return self._unknown_semantic(policy_weight, "semantic_scorer_unavailable")

        # Dense failed unexpectedly: graceful sparse degradation
        if sparse_score > 0.0:
            return (
                FactorResult(
                    factor="semantic",
                    status="scored",
                    rawScore=round(sparse_score, 4),
                    reliability=0.85,
                    policyWeight=policy_weight,
                    effectiveWeight=0.0,
                    evidenceRefs=[item.evidence_id for item in payload.resume.evidence],
                    sparseScore=round(sparse_score, 4),
                ),
                "semantic_dense_provider_fallback_to_sparse",
            )

        return self._unknown_semantic(policy_weight, "semantic_scorer_unavailable")

    async def _semantic_factor_async(
        self, payload: MatchRequest, policy_weight: float
    ) -> tuple[FactorResult, str | None]:
        """Async equivalent of the semantic factor's sparse-score portion."""
        contextual_resume_text = [
            value for value in (payload.resume.profile.headline, payload.resume.profile.summary) if value
        ]
        contextual_resume_text.extend(claim.concept.label for claim in payload.resume.skills)
        contextual_resume_text.extend(item.label for item in payload.resume.career_classifications)
        contextual_resume_text.extend(project.name for project in payload.resume.projects)
        contextual_resume_text.extend(
            text for employment in payload.resume.employment for text in employment.responsibilities
        )
        contextual_resume_text.extend(
            achievement.text
            for employment in payload.resume.employment
            for achievement in employment.achievements
        )
        contextual_resume_text.extend(
            project.description for project in payload.resume.projects if project.description
        )
        contextual_job_text = [value for value in (payload.job.job_title,) if value]
        contextual_job_text.extend(
            item.raw_label for item in payload.job.requirements if isinstance(item, UnresolvedRequirement)
        )
        contextual_job_text.extend(
            item.skill.label for item in payload.job.requirements if isinstance(item, SkillRequirement)
        )
        contextual_job_text.extend(item.text for item in payload.job.responsibilities)
        contextual_job_text.extend(item.label for item in payload.job.career_classifications)
        resume_text = "\n".join(contextual_resume_text) or "\n".join(
            item.text for item in payload.resume.evidence
        )
        job_text = "\n".join(contextual_job_text) or "\n".join(item.text for item in payload.job.evidence)
        if not resume_text or not job_text:
            return self._unknown_semantic(policy_weight, "semantic_input_not_evidenced")
        if payload.matching_policy.bm25_provider_mode == "in_memory":
            sparse_score = bm25_similarity(job_text, resume_text)
        else:
            sparse_score = await self._bm25_provider.score_async(
                query_text=job_text,
                doc_text=resume_text,
                doc_id=payload.resume.document_id or payload.resume.resume_id,
            )

        # The remaining semantic work is CPU-only and shares the established
        # scoring implementation without touching the database again.
        return self._semantic_factor(payload, policy_weight, sparse_score=sparse_score)

    @staticmethod
    def _token_overlap(left: str | None, right: str | None) -> float:
        def tokens(value: str | None) -> set[str]:
            normalized = unicodedata.normalize("NFKD", value or "")
            ascii_value = "".join(char for char in normalized if not unicodedata.combining(char))
            raw_tokens = set(re.findall(r"[a-z0-9+#.]+", ascii_value.casefold()))
            return {ROLE_SYNONYMS.get(t, t) for t in raw_tokens}

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
                    reasonCode=("work_mode_accepted" if compatible else "work_mode_not_accepted"),
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
    def _score_provenance(
        requirements: list[RequirementResult], factors: list[FactorResult]
    ) -> ScoreProvenance:
        scored_factors = [item.factor for item in factors if item.status == "scored"]
        supported = [item for item in requirements if item.reason_code != "requirement_evaluator_unsupported"]
        scored_requirements = [item for item in supported if item.score is not None]
        if scored_factors == ["semantic"]:
            mode = "semantic_only_estimated"
        elif scored_factors:
            mode = "requirement_aware"
        else:
            mode = "unavailable"
        return ScoreProvenance(
            mode=mode,
            scoredFactors=scored_factors,
            supportedRequirementCount=len(supported),
            scoredRequirementCount=len(scored_requirements),
            unknownRequirementCount=sum(item.status == "unknown" for item in requirements),
            totalRequirementCount=len(requirements),
            requirementCoverage=next(
                (
                    item.raw_score
                    for item in factors
                    if item.factor == "requirement_coverage" and item.status == "scored"
                ),
                None,
            ),
            factorContributions={
                item.factor: item.effective_weight * (item.raw_score or 0.0)
                for item in factors
                if item.status == "scored" and item.raw_score is not None
            },
        )

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
