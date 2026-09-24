"""Optional Jev-backed ambiguity analysis for unknown matching requirements.

This module deliberately does not turn an unknown requirement into met/not_met.
Its only job is to decide whether the next safe step is to ask the candidate for
more evidence and, if so, which evidence dimension is missing.
"""

from __future__ import annotations

import logging
import os
from typing import Protocol, runtime_checkable

import httpx

from src.modules.matching.bm25 import bm25_similarity
from src.modules.matching.schemas import (
    ClarificationRequest,
    LanguageRequirement,
    Requirement,
    SkillRequirement,
    UnresolvedRequirement,
)
from src.modules.user_cvs.schemas import CanonicalResume

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://api.typesafe.ai"
_DEFAULT_MODEL = "jev-latest"
_PRIVATE_SECTIONS = {"identity", "contact", "personal", "pii"}


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

    Jev never decides the requirement status or score here. It receives the
    unresolved requirement plus a small, evidence-grounded CV context and
    returns two typed decisions:
      1. whether the next action is to ask the candidate or review existing data;
      2. which evidence dimension is missing.
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
                    "Do not decide met or not_met. Decide only what evidence action is needed "
                    "to resolve the existing unknown result."
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
                            "The existing CV/evidence may already contain enough information, but "
                            "parser, retrieval, taxonomy, or evidence mapping should be reviewed first."
                        ),
                        "no_safe_clarification": (
                            "A candidate clarification would not safely or reliably resolve this requirement."
                        ),
                    },
                },
                "missing_dimension": {
                    "type": "choice",
                    "instructions": (
                        "Which single evidence dimension is most important to obtain or verify next?"
                    ),
                    "criteria": {
                        "duration": "How long the candidate performed or used something.",
                        "proficiency": "Depth or proficiency level for a skill.",
                        "scale": "System, workload, traffic, team, or operational scale.",
                        "responsibility": "The candidate's direct ownership or responsibility.",
                        "education": "Degree, field of study, or education status.",
                        "language_level": "Language proficiency or certification level.",
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
            body = response.json()
            answers = body.get("answers", {})
            next_action = answers.get("next_action", {})
            missing_dimension = answers.get("missing_dimension", {})

            if next_action.get("type") != "choice" or missing_dimension.get("type") != "choice":
                logger.warning("Jev ambiguity response returned unexpected answer types")
                return None
            if next_action.get("choice") != "ask_candidate":
                return None

            action_confidence = float(next_action.get("confidence", 0.0))
            dimension_confidence = float(missing_dimension.get("confidence", 0.0))
            confidence = min(action_confidence, dimension_confidence)
            if confidence < self._confidence_threshold:
                return None

            dimension = str(missing_dimension.get("choice", "other"))
            allowed_dimensions = {
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
                reasonCode="jev_candidate_clarification_needed",
                promptKey=f"matching.clarification.{dimension}",
            )
        except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
            # Matching must remain fail-closed: a Jev outage cannot change the
            # deterministic result or turn an unknown into a scored claim.
            logger.warning("Jev ambiguity analysis failed: %s", exc)
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


def build_ambiguity_analyzer_from_env() -> AmbiguityAnalyzer:
    """Build the optional Jev analyzer without changing default behavior."""

    enabled = os.getenv("JEV_CLARIFICATION_ENABLED", "false").strip().casefold() in {
        "1",
        "true",
        "yes",
        "on",
    }
    api_key = os.getenv("JEV_API_KEY", "").strip()
    if not enabled or not api_key:
        return NoopAmbiguityAnalyzer()

    try:
        timeout_seconds = float(os.getenv("JEV_TIMEOUT_SECONDS", "5"))
        threshold = float(os.getenv("JEV_CLARIFICATION_CONFIDENCE_THRESHOLD", "0.75"))
        max_evidence = int(os.getenv("JEV_MAX_EVIDENCE_ITEMS", "5"))
    except ValueError:
        logger.warning("Invalid Jev environment configuration; disabling ambiguity analyzer")
        return NoopAmbiguityAnalyzer()

    return JevAmbiguityAnalyzer(
        api_key=api_key,
        model=os.getenv("JEV_MODEL", _DEFAULT_MODEL).strip() or _DEFAULT_MODEL,
        base_url=os.getenv("JEV_BASE_URL", _DEFAULT_BASE_URL).strip() or _DEFAULT_BASE_URL,
        timeout_seconds=max(0.1, timeout_seconds),
        confidence_threshold=threshold,
        max_evidence_items=max_evidence,
    )
