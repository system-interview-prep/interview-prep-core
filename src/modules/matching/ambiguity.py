"""Optional Jev-backed clarification analysis for unknown requirements.

The matching engine remains authoritative for requirement status and scoring.
This module runs only after matching has already produced ``unknown`` and may
recommend which factual detail to ask the candidate for next. It never changes
``met``/``not_met``, eligibility, suitability, or fit-band decisions.
"""

from __future__ import annotations

import logging
import os
from typing import Literal, Protocol, runtime_checkable

import httpx
from pydantic import Field

from src.modules.matching.bm25 import bm25_similarity
from src.modules.matching.schemas import (
    LanguageRequirement,
    MatchRequest,
    MatchResult,
    Requirement,
    SkillRequirement,
    UnresolvedRequirement,
)
from src.modules.user_cvs.schemas import CanonicalModel, CanonicalResume

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


class ClarificationRequest(CanonicalModel):
    """A safe request for additional candidate-provided evidence.

    ``prompt_key`` intentionally points to an application-owned template. Jev
    chooses only the missing evidence dimension; it does not generate user-facing
    prose and it does not manufacture evidence.
    """

    requirement_id: str = Field(min_length=1)
    missing_dimension: MissingDimension
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_refs: list[str] = Field(default_factory=list)
    reason_code: Literal["jev_candidate_clarification_needed"] = (
        "jev_candidate_clarification_needed"
    )
    prompt_key: str = Field(min_length=1)


@runtime_checkable
class AmbiguityAnalyzer(Protocol):
    """Analyzes why an already-unknown requirement still needs evidence."""

    def analyze(
        self,
        requirement: Requirement,
        resume: CanonicalResume,
    ) -> ClarificationRequest | None:
        ...


class NoopAmbiguityAnalyzer:
    """Default analyzer that preserves current matching behavior."""

    def analyze(
        self,
        requirement: Requirement,
        resume: CanonicalResume,
    ) -> ClarificationRequest | None:
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
    ) -> ClarificationRequest | None:
        requirement_text = self._requirement_text(requirement)
        evidence = self._select_evidence(requirement_text, resume)
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
            next_action = answers.get("next_action", {})
            missing_dimension = answers.get("missing_dimension", {})

            if next_action.get("type") != "choice" or missing_dimension.get("type") != "choice":
                logger.warning("Jev clarification response returned unexpected answer types")
                return None
            if next_action.get("choice") != "ask_candidate":
                return None

            action_confidence = float(next_action.get("confidence", 0.0))
            dimension_confidence = float(missing_dimension.get("confidence", 0.0))
            confidence = min(action_confidence, dimension_confidence)
            if confidence < self._confidence_threshold:
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

            return ClarificationRequest(
                requirementId=requirement.requirement_id,
                missingDimension=dimension,
                confidence=round(confidence, 4),
                evidenceRefs=[item.evidence_id for item in evidence],
                promptKey=f"matching.clarification.{dimension}",
            )
        except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
            logger.warning("Jev clarification analysis failed: %s", exc)
            return None

    def _select_evidence(self, requirement_text: str, resume: CanonicalResume):
        candidates = [
            item
            for item in resume.evidence
            if item.text and str(item.section or "").casefold() not in _PRIVATE_SECTIONS
        ]
        ranked = sorted(
            candidates,
            key=lambda item: bm25_similarity(requirement_text, item.text),
            reverse=True,
        )
        return ranked[: self._max_evidence_items]

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


def build_clarification_requests(
    payload: MatchRequest,
    result: MatchResult,
    analyzer: AmbiguityAnalyzer,
) -> list[ClarificationRequest]:
    """Build clarification requests only for requirements already marked unknown.

    This is intentionally a post-processing step. ``result`` is read-only here;
    no scores, statuses, eligibility, or fit-band values are changed.
    """

    results_by_id = {item.requirement_id: item for item in result.requirement_results}
    requests: list[ClarificationRequest] = []
    for requirement in payload.job.requirements:
        requirement_result = results_by_id.get(requirement.requirement_id)
        if requirement_result is None or requirement_result.status != "unknown":
            continue
        clarification = analyzer.analyze(requirement, payload.resume)
        if clarification is not None:
            requests.append(clarification)
    return requests


def build_ambiguity_analyzer_from_env() -> AmbiguityAnalyzer:
    """Build the optional Jev analyzer without changing default behavior."""

    enabled = os.getenv("JEV_CLARIFICATION_ENABLED", "false").strip().casefold() in {
        "1",
        "true",
        "yes",
        "on",
    }
    api_key = (
        os.getenv("TYPESAFE_API_KEY", "").strip()
        or os.getenv("JEV_API_KEY", "").strip()
    )
    if not enabled or not api_key:
        return NoopAmbiguityAnalyzer()

    try:
        timeout_seconds = float(os.getenv("JEV_TIMEOUT_SECONDS", "5"))
        threshold = float(os.getenv("JEV_CLARIFICATION_CONFIDENCE_THRESHOLD", "0.75"))
        max_evidence = int(os.getenv("JEV_MAX_EVIDENCE_ITEMS", "5"))
    except ValueError:
        logger.warning("Invalid Jev environment configuration; disabling clarification analyzer")
        return NoopAmbiguityAnalyzer()

    return JevAmbiguityAnalyzer(
        api_key=api_key,
        model=os.getenv("TYPESAFE_DEFAULT_MODEL", os.getenv("JEV_MODEL", _DEFAULT_MODEL)).strip()
        or _DEFAULT_MODEL,
        base_url=os.getenv("TYPESAFE_BASE_URL", os.getenv("JEV_BASE_URL", _DEFAULT_BASE_URL)).strip()
        or _DEFAULT_BASE_URL,
        timeout_seconds=max(0.1, timeout_seconds),
        confidence_threshold=threshold,
        max_evidence_items=max_evidence,
    )
