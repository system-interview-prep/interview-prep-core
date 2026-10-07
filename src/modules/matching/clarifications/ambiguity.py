"""Optional Jev-backed clarification analysis for unknown requirements.

The matching engine remains authoritative for requirement status and scoring.
This module runs only after matching has already produced ``unknown`` and may
recommend which factual detail to ask the candidate for next. It never changes
``met``/``not_met``, eligibility, suitability, or fit-band decisions.
"""

from __future__ import annotations

import logging
from typing import Literal, Protocol, runtime_checkable

import httpx

from src.core.trace_logging import trace_event
from src.modules.matching.clarifications.question_generation import (
    ClarificationPlan,
    ClarificationQuestion,
    ClarificationQuestionService,
)
from src.modules.matching.domain.schemas import (
    LanguageRequirement,
    MatchRequest,
    MatchResult,
    Requirement,
    SkillRequirement,
    UnresolvedRequirement,
)
from src.modules.matching.retrieval.bm25 import bm25_similarity
from src.modules.user_cvs.schemas import CanonicalResume

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://api.typesafe.ai"
_DEFAULT_MODEL = "jev-latest"
_PRIVATE_SECTIONS = {"identity", "contact", "personal", "pii"}

MissingDimension = Literal[
    "duration",
    "proficiency",
    "scale",
    "responsibility",
    "education",
    "language_level",
    "certification",
    "experience_context",
    "other",
]


@runtime_checkable
class AmbiguityAnalyzer(Protocol):
    """Analyzes why an already-unknown requirement still needs evidence."""

    def analyze(
        self,
        requirement: Requirement,
        resume: CanonicalResume,
    ) -> ClarificationPlan | None:
        ...


class NoopAmbiguityAnalyzer:
    """Default analyzer that preserves current matching behavior."""

    def analyze(
        self,
        requirement: Requirement,
        resume: CanonicalResume,
    ) -> ClarificationPlan | None:
        del requirement, resume
        return None


class JevAmbiguityAnalyzer:
    """Uses Jev only to route unknowns toward candidate clarification.

    Jev receives the requirement plus a small evidence-grounded CV context and
    answers two closed questions in parallel:
      1. whether asking the candidate is the safest next action;
      2. which evidence dimension is missing.

    The response is ignored unless both choices are confident enough. A network
    failure or unexpected response therefore cannot alter a matching result.
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str = _DEFAULT_MODEL,
        base_url: str = _DEFAULT_BASE_URL,
        timeout_seconds: float = 5.0,
        confidence_threshold: float = 0.75,
        max_evidence_items: int = 5,
        client: httpx.Client | None = None,
    ) -> None:
        self._model = model
        self._confidence_threshold = min(1.0, max(0.0, confidence_threshold))
        self._max_evidence_items = max(1, max_evidence_items)
        self._client = client or httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout_seconds,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )

    def analyze(
        self,
        requirement: Requirement,
        resume: CanonicalResume,
    ) -> ClarificationPlan | None:
        requirement_text = self._requirement_text(requirement)
        evidence = self._select_evidence(requirement_text, resume, self._max_evidence_items)
        trace_event(
            "clarification",
            "jev_analysis_started",
            requirement_id=requirement.requirement_id,
            model=self._model,
            confidence_threshold=self._confidence_threshold,
            evidence_count=len(evidence),
        )
        payload = {
            "model": self._model,
            "state": {
                "requirement": requirement_text,
                "current_status": "unknown",
                "candidate_evidence": [
                    {
                        "evidence_id": item.evidence_id,
                        "section": item.section,
                        "text": item.text,
                    }
                    for item in evidence
                ],
                "policy": (
                    "Do not decide whether the requirement is met. Decide only whether "
                    "candidate-provided factual evidence is needed next and what kind."
                ),
            },
            "questions": {
                "next_action": {
                    "type": "choice",
                    "instructions": (
                        "What is the safest next action for resolving this unknown requirement?"
                    ),
                    "criteria": {
                        "ask_candidate": (
                            "A factual detail is missing and should be supplied by the candidate."
                        ),
                        "review_existing_evidence": (
                            "Existing evidence may already be sufficient; parser, retrieval, taxonomy, "
                            "or evidence mapping should be reviewed before asking the candidate."
                        ),
                        "no_safe_clarification": (
                            "Candidate clarification would not safely or reliably resolve the requirement."
                        ),
                    },
                },
                "missing_dimension": {
                    "type": "choice",
                    "instructions": (
                        "Which single factual evidence dimension is most important to obtain or verify next?"
                    ),
                    "criteria": {
                        "duration": "How long the candidate performed or used something.",
                        "proficiency": "Depth or proficiency level for a skill.",
                        "scale": "System, workload, traffic, team, or operational scale.",
                        "responsibility": "The candidate's direct ownership or responsibility.",
                        "education": "Degree, field of study, or education status.",
                        "language_level": "Language proficiency or language-test level.",
                        "certification": "A required professional or technical certification.",
                        "experience_context": (
                            "Concrete project, production, domain, or hands-on experience context."
                        ),
                        "other": "Another factual dimension not covered above.",
                    },
                },
            },
        }

        try:
            response = self._client.post("/v1/systemone", json=payload)
            response.raise_for_status()
            answers = response.json().get("answers", {})
            if not isinstance(answers, dict):
                logger.warning("Jev clarification response returned invalid answers payload")
                trace_event(
                    "clarification",
                    "jev_analysis_rejected",
                    requirement_id=requirement.requirement_id,
                    reason="invalid_answers_payload",
                )
                return None
            next_action = answers.get("next_action", {})
            missing_dimension = answers.get("missing_dimension", {})

            if (
                not isinstance(next_action, dict)
                or not isinstance(missing_dimension, dict)
                or next_action.get("type") != "choice"
                or missing_dimension.get("type") != "choice"
            ):
                logger.warning("Jev clarification response returned unexpected answer types")
                trace_event(
                    "clarification",
                    "jev_analysis_rejected",
                    requirement_id=requirement.requirement_id,
                    reason="unexpected_answer_types",
                )
                return None
            if next_action.get("choice") != "ask_candidate":
                trace_event(
                    "clarification",
                    "jev_analysis_rejected",
                    requirement_id=requirement.requirement_id,
                    reason="next_action_not_ask_candidate",
                    next_action=next_action.get("choice"),
                    confidence=next_action.get("confidence"),
                )
                return None

            action_confidence = float(next_action.get("confidence", 0.0))
            dimension_confidence = float(missing_dimension.get("confidence", 0.0))
            confidence = min(action_confidence, dimension_confidence)
            if confidence < self._confidence_threshold:
                trace_event(
                    "clarification",
                    "jev_analysis_rejected",
                    requirement_id=requirement.requirement_id,
                    reason="confidence_below_threshold",
                    action_confidence=action_confidence,
                    dimension_confidence=dimension_confidence,
                    confidence=confidence,
                    confidence_threshold=self._confidence_threshold,
                )
                return None

            dimension = str(missing_dimension.get("choice", "other"))
            allowed_dimensions: set[str] = {
                "duration",
                "proficiency",
                "scale",
                "responsibility",
                "education",
                "language_level",
                "certification",
                "experience_context",
                "other",
            }
            if dimension not in allowed_dimensions:
                dimension = "other"

            plan = ClarificationPlan(
                requirementId=requirement.requirement_id,
                missingDimension=dimension,
                confidence=round(confidence, 4),
                evidenceRefs=[item.evidence_id for item in evidence],
                requirementText=requirement_text,
                subjectTerms=self._subject_terms(requirement),
                evidenceTexts=[item.text for item in evidence],
            )
            trace_event(
                "clarification",
                "jev_analysis_approved",
                requirement_id=requirement.requirement_id,
                missing_dimension=plan.missing_dimension,
                confidence=plan.confidence,
                evidence_count=len(plan.evidence_refs),
            )
            return plan
        except (httpx.HTTPError, ValueError, TypeError, KeyError, AttributeError) as exc:
            logger.warning("Jev clarification analysis failed: %s", exc)
            trace_event(
                "clarification",
                "jev_analysis_failed",
                requirement_id=requirement.requirement_id,
                reason=type(exc).__name__,
            )
            return None

    @staticmethod
    def _select_evidence(
        requirement_text: str,
        resume: CanonicalResume,
        max_evidence_items: int = 5,
    ):
        candidates = [
            item
            for item in resume.evidence
            if item.text and str(item.section or "").casefold() not in _PRIVATE_SECTIONS
        ]
        ranked = sorted(
            (
                (bm25_similarity(requirement_text, item.text), item)
                for item in candidates
            ),
            key=lambda pair: pair[0],
            reverse=True,
        )
        return [item for score, item in ranked if score > 0][:max(1, max_evidence_items)]

    @staticmethod
    def _requirement_text(requirement: Requirement) -> str:
        if isinstance(requirement, UnresolvedRequirement):
            return requirement.raw_label
        if isinstance(requirement, SkillRequirement):
            label = requirement.skill.label or requirement.skill.concept_id
            if requirement.operator == "gte":
                return f"{label}; minimum experience months: {requirement.minimum_experience_months}"
            if requirement.operator == "proficiency_gte":
                return f"{label}; minimum proficiency: {requirement.minimum_proficiency_level}"
            return label
        if isinstance(requirement, LanguageRequirement):
            if requirement.operator == "equal":
                return (
                    f"language {requirement.language_code}; required level: "
                    f"{requirement.minimum_level}"
                )
            return f"language {requirement.language_code} required"
        return requirement.requirement_id

    @staticmethod
    def _subject_terms(requirement: Requirement) -> list[str]:
        if isinstance(requirement, SkillRequirement):
            return [requirement.skill.label] if requirement.skill.label else []
        if isinstance(requirement, UnresolvedRequirement):
            return [item.label for item in requirement.atomic_concepts if item.label]
        return []


def build_clarification_plans(
    payload: MatchRequest,
    result: MatchResult,
    analyzer: AmbiguityAnalyzer,
    requirement_ids: set[str] | None = None,
) -> list[ClarificationPlan]:
    """Run the Jev eligibility gate for requirements already marked unknown.

    This is intentionally a post-processing step. ``result`` is read-only here;
    no scores, statuses, eligibility, or fit-band values are changed.
    """

    results_by_id = {item.requirement_id: item for item in result.requirement_results}
    plans: list[ClarificationPlan] = []
    for requirement in payload.job.requirements:
        if requirement_ids is not None and requirement.requirement_id not in requirement_ids:
            continue
        requirement_result = results_by_id.get(requirement.requirement_id)
        if requirement_result is None or requirement_result.status != "unknown":
            continue
        plan = analyzer.analyze(requirement, payload.resume)
        if plan is not None:
            plans.append(plan)
        else:
            trace_event(
                "clarification",
                "jev_plan_unavailable",
                requirement_id=requirement.requirement_id,
                reason="no_approved_plan",
            )
    return plans


def build_clarification_requests_from_plans(
    plans: list[ClarificationPlan],
    question_service: ClarificationQuestionService | None,
) -> list[ClarificationQuestion]:
    """Generate questions after Jev has explicitly approved candidate clarification."""

    if question_service is None:
        trace_event(
            "clarification",
            "question_service_unavailable",
            reason="provider_not_configured_or_initialization_failed",
        )
        return []
    requests: list[ClarificationQuestion] = []
    for plan in plans:
        question = question_service.create(plan)
        if question is not None:
            requests.append(question)
    return requests


def build_clarification_requests(
    payload: MatchRequest,
    result: MatchResult,
    analyzer: AmbiguityAnalyzer,
    question_service: ClarificationQuestionService | None,
) -> list[ClarificationQuestion]:
    """Backward-compatible one-shot Jev analysis plus question generation."""

    plans = build_clarification_plans(payload, result, analyzer)
    return build_clarification_requests_from_plans(plans, question_service)


def build_ambiguity_analyzer_from_env() -> AmbiguityAnalyzer:
    """Build the optional Jev analyzer without changing default behavior."""

    from src.core.config import Settings

    settings = Settings()
    enabled = settings.jev_clarification_enabled
    api_key = (settings.typesafe_api_key or "").strip()
    if not enabled or not api_key:
        trace_event(
            "clarification",
            "jev_service_unavailable",
            reason="disabled" if not enabled else "missing_api_key",
        )
        return NoopAmbiguityAnalyzer()

    return JevAmbiguityAnalyzer(
        api_key=api_key,
        model=settings.typesafe_default_model.strip() or _DEFAULT_MODEL,
        base_url=settings.typesafe_base_url.strip() or _DEFAULT_BASE_URL,
        timeout_seconds=settings.jev_timeout_seconds,
        confidence_threshold=settings.jev_clarification_confidence_threshold,
        max_evidence_items=settings.jev_max_evidence_items,
    )
