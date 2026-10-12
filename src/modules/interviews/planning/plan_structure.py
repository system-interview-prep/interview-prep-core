"""Deterministic structural contract for the P1 interview planner.

This module turns already-grounded JD requirements and matching outcomes into
an explainable interview agenda. It deliberately does not select or generate
questions.
"""

import re
from collections.abc import Mapping
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


# Level words in posting titles the JD parser does not map to a seniority:
# "Android Engineer I", "Staff Engineer", "Principal Developer".
_TITLE_LEVELS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(intern|internship|graduate|entry[- ]level|thực tập)\b", re.IGNORECASE), "foundational"),
    (re.compile(r"\b(staff|principal|distinguished|architect)\b", re.IGNORECASE), "advanced"),
    (re.compile(r"\b(?:engineer|developer|programmer)\s+(?:III|IV|V|3|4|5)\b", re.IGNORECASE), "advanced"),
    (re.compile(r"\b(?:engineer|developer|programmer)\s+(?:II|2)\b", re.IGNORECASE), "intermediate"),
    (re.compile(r"\b(?:engineer|developer|programmer)\s+(?:I|1)\b", re.IGNORECASE), "foundational"),
)


def _level_from_years(min_years: float) -> str:
    if min_years <= 1:
        return "foundational"
    if min_years < 5:
        return "intermediate"
    return "advanced"


def derive_difficulty(job: CanonicalJob, *, listing: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Derive the session difficulty from what the job posting states.

    Still JD-only: never inferred from CV prose or matching scores. Most
    postings carry no seniority word, which used to leave difficulty
    "unspecified" and switched it off for the selector (54 of 55 plans), so
    the posting's other signals are tried in order:

    1. ``listing.seniority``: the listing column (ingest copies the parsed
       value; an admin correction lands here too), so it wins over the parser;
    2. the parsed seniority;
    3. a level in the title ("Engineer II", "Staff", "Principal");
    4. ``listing.experience_min_years``: <=1 foundational, <5 intermediate;
    5. otherwise "intermediate", marked as a default so it stays visible.
    """

    listing = listing or {}
    override = str(listing.get("seniority") or "").strip().lower()
    if override in _SENIORITY_DIFFICULTY:
        return {"level": _SENIORITY_DIFFICULTY[override], "source": "job_listing_seniority", "seniority": override}
    if job.seniority is not None:
        return {"level": _SENIORITY_DIFFICULTY[job.seniority], "source": "job_seniority", "seniority": job.seniority}
    title = str(listing.get("title") or job.job_title or "")
    for pattern, level in _TITLE_LEVELS:
        if pattern.search(title):
            return {"level": level, "source": "job_title_level", "seniority": None, "title": title}
    min_years = listing.get("experience_min_years")
    if min_years is not None:
        try:
            years = float(min_years)
        except (TypeError, ValueError):
            years = None
        if years is not None and years >= 0:
            return {
                "level": _level_from_years(years),
                "source": "job_min_experience_years",
                "seniority": None,
                "minExperienceYears": years,
            }
    return {"level": "intermediate", "source": "default_no_job_signal", "seniority": None}


def _allocate_minutes(duration_minutes: int, weights: list[int]) -> list[int]:
    """Allocate whole minutes deterministically and preserve the exact total."""

    if duration_minutes <= 0:
        raise ValueError("Interview duration is too short for the planner section contract")

    # Every section gets a minute when there is room. Demo sessions shorter
    # than the number of sections get 0-minute sections instead of failing.
    floor = 1 if duration_minutes >= len(weights) else 0
    minutes = [floor] * len(weights)
    remaining = duration_minutes - sum(minutes)
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


def _requirement_label(requirement: Any) -> str:
    if isinstance(requirement, SkillRequirement):
        return requirement.skill.label
    if isinstance(requirement, LanguageRequirement):
        return requirement.language_code
    if isinstance(requirement, UnresolvedRequirement):
        return requirement.raw_label
    return "Requirement"


def _requirement_group_operator(requirement: Any) -> str:
    if isinstance(requirement, UnresolvedRequirement):
        return requirement.group_operator or "atomic"
    return "atomic"


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
                "label": _requirement_label(requirement),
                "groupOperator": _requirement_group_operator(requirement),
                "status": status,
                "reasonCode": result.reason_code if result is not None else "match_result_missing",
                "conceptIds": concept_ids,
                "conceptResults": (
                    [
                        {
                            "conceptId": concept_result.concept_id,
                            "label": concept_result.label,
                            "status": concept_result.status,
                            "confidence": concept_result.confidence,
                            "reasonCode": concept_result.reason_code,
                            "candidateEvidenceRefs": list(concept_result.evidence_refs),
                        }
                        for concept_result in result.concept_results
                    ]
                    if result is not None
                    else []
                ),
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
