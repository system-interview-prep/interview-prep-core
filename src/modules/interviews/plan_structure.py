"""Deterministic structural contract for the P1 interview planner.

This module turns already-grounded JD requirements and matching outcomes into
an explainable interview agenda. It deliberately does not select or generate
questions.
"""

from typing import Any

from src.modules.matching.schemas import (
    CanonicalJob,
    LanguageRequirement,
    MatchResult,
    SkillRequirement,
    UnresolvedRequirement,
)

_SENIORITY_DIFFICULTY = {
    "intern": "foundational",
    "fresher": "foundational",
    "junior": "foundational",
    "mid": "intermediate",
    "senior": "advanced",
    "lead": "advanced",
    "manager": "advanced",
}


def derive_difficulty(job: CanonicalJob) -> dict[str, Any]:
    """Derive difficulty only from explicit JD seniority.

    P1 does not infer a candidate's level from CV prose and does not silently
    upgrade difficulty from matching scores.
    """

    if job.seniority is None:
        return {
            "level": "unspecified",
            "source": "job_seniority_missing",
            "seniority": None,
        }
    return {
        "level": _SENIORITY_DIFFICULTY[job.seniority],
        "source": "job_seniority",
        "seniority": job.seniority,
    }


def _allocate_minutes(duration_minutes: int, weights: list[int]) -> list[int]:
    """Allocate whole minutes deterministically and preserve the exact total."""

    if duration_minutes < len(weights):
        raise ValueError("Interview duration is too short for the planner section contract")

    minutes = [1] * len(weights)
    remaining = duration_minutes - len(weights)
    total_weight = sum(weights)

    while remaining:
        index = max(
            range(len(weights)),
            key=lambda idx: (
                weights[idx] / total_weight * duration_minutes - minutes[idx],
                weights[idx],
                -idx,
            ),
        )
        minutes[index] += 1
        remaining -= 1
    return minutes


def build_sections(duration_minutes: int) -> list[dict[str, Any]]:
    """Create a stable agenda; P2 will later fill question slots."""

    definitions = [
        ("warmup", "Warm-up", 10),
        ("core", "Core competencies", 50),
        ("gap_validation", "Evidence gaps & deep dive", 30),
        ("closing", "Closing", 10),
    ]
    minutes = _allocate_minutes(duration_minutes, [item[2] for item in definitions])
    return [
        {
            "sectionId": section_id,
            "label": label,
            "durationMinutes": allocated,
            "purpose": purpose,
        }
        for (section_id, label, _), allocated, purpose in zip(
            definitions,
            minutes,
            [
                "Establish interview context without scoring unsupported claims.",
                "Assess the JD-backed competencies with the highest grounded importance.",
                "Validate unknown/not-met requirements and deepen evidence where needed.",
                "Reserve time to conclude the structured interview cleanly.",
            ],
            strict=True,
        )
    ]


def _requirement_concept_ids(requirement: Any) -> list[str]:
    if isinstance(requirement, SkillRequirement):
        return [requirement.skill.concept_id]
    if isinstance(requirement, UnresolvedRequirement):
        return [item.concept_id for item in requirement.atomic_concepts]
    return []


def _requirement_kind(requirement: Any) -> str:
    if isinstance(requirement, SkillRequirement):
        return "skill"
    if isinstance(requirement, LanguageRequirement):
        return "language"
    if isinstance(requirement, UnresolvedRequirement):
        return requirement.kind
    return "other"


def build_evaluation_targets(
    *,
    job: CanonicalJob,
    match: MatchResult,
) -> list[dict[str, Any]]:
    """Cover every JD requirement, including non-taxonomy requirements.

    Competency targets remain taxonomy-backed. Evaluation targets are broader:
    GPA, education, language credentials and other grounded requirements still
    receive an explicit validation target instead of disappearing from P1.
    """

    results = {item.requirement_id: item for item in match.requirement_results}
    targets: list[dict[str, Any]] = []

    for requirement in job.requirements:
        result = results.get(requirement.requirement_id)
        status = result.status if result is not None else "unknown"
        concept_ids = _requirement_concept_ids(requirement)
        targets.append(
            {
                "requirementId": requirement.requirement_id,
                "priority": requirement.priority,
                "kind": _requirement_kind(requirement),
                "status": status,
                "reasonCode": result.reason_code if result is not None else "match_result_missing",
                "conceptIds": concept_ids,
                "jobEvidenceRefs": [requirement.source_evidence_ref],
                "candidateEvidenceRefs": list(result.evidence_refs) if result is not None else [],
                "evaluationMode": (
                    "competency"
                    if concept_ids
                    else "requirement_validation"
                ),
                "attention": (
                    "validate_gap"
                    if status in {"unknown", "not_met"}
                    else "verify_claim"
                    if status == "met"
                    else "not_applicable"
                ),
            }
        )

    return targets


def validate_must_have_coverage(
    *,
    job: CanonicalJob,
    evaluation_targets: list[dict[str, Any]],
) -> None:
    """Fail closed if a must-have requirement has no evaluation target."""

    covered = {item["requirementId"] for item in evaluation_targets}
    missing = [
        requirement.requirement_id
        for requirement in job.requirements
        if requirement.priority == "must_have"
        and requirement.requirement_id not in covered
    ]
    if missing:
        raise ValueError(
            "Planner did not cover must-have requirements: " + ", ".join(sorted(missing))
        )
