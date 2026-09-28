"""Deterministic P1 interview planner.

P1 converts grounded CV/JD matching context into a persisted competency agenda.
It does not select questions and it does not ask an LLM to invent competencies.
"""

import hashlib
import json
import math
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.interviews.plan_structure import (
    build_evaluation_targets,
    build_sections,
    derive_difficulty,
    validate_must_have_coverage,
)
from src.modules.interviews.planner_config import (
    PlannerPolicyConfig,
    resolve_strict_hands_on_precedence,
)
from src.modules.job_descriptions.schemas import CanonicalJobDescription
from src.modules.matching.facade import canonical_job_from_description, evaluate_match
from src.modules.matching.schemas import (
    CanonicalJob,
    LanguageRequirement,
    MatchRequest,
    MatchResult,
    SkillRequirement,
    TaxonomyRef,
    UnresolvedRequirement,
)
from src.modules.user_cvs.schemas import CanonicalResume

PLANNER_POLICY_VERSION = "interview-planner-v1"
PLANNER_POLICY_VERSION_DYNAMIC = "interview-planner-v2-dynamic"

_PRIORITY_WEIGHT = {
    "must_have": 1.0,
    "nice_to_have": 0.45,
    "context": 0.20,
}
_STATUS_BOOST = {
    "not_met": 1.35,
    "unknown": 1.20,
    "met": 1.00,
    "not_applicable": 0.0,
}
_MAX_QUESTIONS_PER_TARGET = 3


@dataclass
class _CandidateTarget:
    concept: TaxonomyRef
    raw_weight: float = 0.0
    requirement_ids: list[str] = field(default_factory=list)
    priorities: list[str] = field(default_factory=list)
    match_statuses: list[str] = field(default_factory=list)
    source_evidence_refs: list[str] = field(default_factory=list)
    source: str = "job_requirement"

    def add_requirement(
        self,
        *,
        requirement_id: str,
        priority: str,
        match_status: str,
        source_evidence_ref: str,
        weight: float,
    ) -> None:
        self.raw_weight += weight
        if requirement_id not in self.requirement_ids:
            self.requirement_ids.append(requirement_id)
        if priority not in self.priorities:
            self.priorities.append(priority)
        if match_status not in self.match_statuses:
            self.match_statuses.append(match_status)
        if source_evidence_ref not in self.source_evidence_refs:
            self.source_evidence_refs.append(source_evidence_ref)


def _question_budget(duration_minutes: int) -> int:
    """Ma trận phân bổ câu hỏi kỹ thuật theo thời lượng (Question Allocation Matrix).

    Tổng số câu phỏng vấn thực tế = technical budget + 2 câu mở đầu (Turn 0: Warm-up, Turn 1: Validate CV).
    - Gói 15 phút (Flash Screen): 2 câu kỹ thuật -> Tổng 4 câu
    - Gói 25 phút (Standard, 20-30m): 4 câu kỹ thuật -> Tổng 6 câu
    - Gói 45 phút (Deep Dive, 35-50m): 6 câu kỹ thuật -> Tổng 8 câu
    - Trên 50 phút: tối đa 8 câu kỹ thuật -> Tổng 10 câu
    """
    if duration_minutes <= 15:
        return 2
    elif duration_minutes <= 30:
        return 4
    elif duration_minutes <= 50:
        return 6
    return min(8, max(3, round((duration_minutes - 10) / 5)))


def _requirement_concepts(requirement: Any) -> list[TaxonomyRef]:
    if isinstance(requirement, SkillRequirement):
        return [requirement.skill]
    if isinstance(requirement, UnresolvedRequirement):
        return list(requirement.atomic_concepts)
    if isinstance(requirement, LanguageRequirement):
        return []
    return []


def _status_for_concept(result: Any, concept_id: str) -> str:
    if result is None:
        return "unknown"
    for concept_result in result.concept_results:
        if concept_result.concept_id == concept_id:
            return concept_result.status
    return result.status


def _allocate_question_counts(weights: list[float], budget: int) -> list[int]:
    """Allocate an integer agenda deterministically.

    Every selected competency gets one question first. Remaining slots are
    assigned by normalized importance with a small per-target cap so one
    competency cannot consume the entire interview.
    """
    if not weights or budget <= 0:
        return []

    counts = [1 for _ in weights]
    remaining = max(0, budget - len(counts))
    if remaining == 0:
        return counts

    total = sum(weights) or 1.0
    normalized = [weight / total for weight in weights]

    while remaining > 0:
        eligible = [
            index
            for index, count in enumerate(counts)
            if count < _MAX_QUESTIONS_PER_TARGET
        ]
        if not eligible:
            break
        # Prefer the target with the largest deficit versus its ideal share.
        index = max(
            eligible,
            key=lambda idx: (
                normalized[idx] * budget - counts[idx],
                normalized[idx],
                -idx,
            ),
        )
        counts[index] += 1
        remaining -= 1

    return counts


def _derive_competency_plan_legacy(
    *,
    job: CanonicalJob,
    match: MatchResult,
    duration_minutes: int,
) -> dict[str, Any]:
    """Legacy fixed question budget algorithm (interview-planner-v1)."""
    result_by_id = {item.requirement_id: item for item in match.requirement_results}
    candidates: dict[tuple[str, str], _CandidateTarget] = {}
    non_competency_requirement_ids: list[str] = []
    had_taxonomy_concepts = False

    for requirement in job.requirements:
        concepts = _requirement_concepts(requirement)
        if not concepts:
            non_competency_requirement_ids.append(requirement.requirement_id)
            continue
        had_taxonomy_concepts = True

        result = result_by_id.get(requirement.requirement_id)
        priority_weight = _PRIORITY_WEIGHT.get(requirement.priority, 0.0)
        concept_count = len(concepts)

        for concept in concepts:
            match_status = _status_for_concept(result, concept.concept_id)
            status_boost = _STATUS_BOOST.get(match_status, 1.0)
            if status_boost <= 0.0:
                continue
            per_concept_weight = priority_weight * status_boost / concept_count

            key = (concept.taxonomy_version, concept.concept_id)
            target = candidates.get(key)
            if target is None:
                target = _CandidateTarget(concept=concept)
                candidates[key] = target
            target.add_requirement(
                requirement_id=requirement.requirement_id,
                priority=requirement.priority,
                match_status=match_status,
                source_evidence_ref=requirement.source_evidence_ref,
                weight=per_concept_weight,
            )

    if not candidates and not had_taxonomy_concepts:
        classifications = sorted(
            job.career_classifications,
            key=lambda item: (not item.is_primary, -item.confidence, item.code),
        )
        for classification in classifications[:2]:
            concept = TaxonomyRef(
                conceptId=classification.code,
                scheme="career",
                taxonomyVersion=classification.taxonomy_version,
                label=classification.label,
            )
            candidates[(concept.taxonomy_version, concept.concept_id)] = _CandidateTarget(
                concept=concept,
                raw_weight=1.0 if classification.is_primary else 0.5,
                source="career_classification_fallback",
            )

    if not candidates:
        raise ValueError("No applicable taxonomy-backed interview competencies could be derived from the job")

    budget = _question_budget(duration_minutes)
    ranked = sorted(
        candidates.values(),
        key=lambda item: (
            0 if "must_have" in item.priorities else 1,
            -item.raw_weight,
            item.concept.taxonomy_version,
            item.concept.concept_id,
        ),
    )
    selected = ranked[: min(len(ranked), budget)]
    raw_weights = [max(item.raw_weight, 0.0001) for item in selected]
    total_weight = sum(raw_weights)
    normalized_weights = [weight / total_weight for weight in raw_weights]
    counts = _allocate_question_counts(normalized_weights, budget)

    targets = []
    for selection_rank, (item, importance, target_count) in enumerate(
        zip(selected, normalized_weights, counts, strict=True)
    ):
        targets.append(
            {
                "selectionRank": selection_rank,
                "taxonomyVersion": item.concept.taxonomy_version,
                "conceptId": item.concept.concept_id,
                "label": item.concept.label,
                "importance": round(importance, 6),
                "targetQuestionCount": target_count,
                "rationale": {
                    "source": item.source,
                    "requirementIds": item.requirement_ids,
                    "priorities": item.priorities,
                    "matchStatuses": item.match_statuses,
                    "jobEvidenceRefs": item.source_evidence_refs,
                },
            }
        )

    evaluation_targets = build_evaluation_targets(job=job, match=match)
    validate_must_have_coverage(job=job, evaluation_targets=evaluation_targets)

    strengths_to_verify = [
        t["label"] or t["conceptId"]
        for t in targets
        if "met" in (t.get("rationale", {}).get("matchStatuses") or [])
    ]
    gap_competencies = [
        t["label"] or t["conceptId"]
        for t in targets
        if any(s in ("not_met", "unknown") for s in (t.get("rationale", {}).get("matchStatuses") or []))
    ]

    plan = {
        "policyVersion": PLANNER_POLICY_VERSION,
        "questionBudget": budget,
        "targetQuestionCount": sum(item["targetQuestionCount"] for item in targets),
        "difficulty": derive_difficulty(job),
        "sections": build_sections(duration_minutes),
        "targets": targets,
        "evaluationTargets": evaluation_targets,
        "nonCompetencyRequirementIds": non_competency_requirement_ids,
        "candidateMatrix": {
            "strengthsToVerify": strengths_to_verify,
            "gapCompetencies": gap_competencies,
        },
    }
    canonical = json.dumps(plan, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    plan["fingerprint"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return plan


_CODING_KEYWORDS = {"coding_problem", "live_coding", "algorithm", "data_structures", "coding"}


def _is_coding_target(concept: TaxonomyRef, requirements: list[Any]) -> bool:
    """Classify preliminarily whether a canonical concept target is CODING or TEXT.

    Checks tags, labels, and evidence metadata without querying Question Bank.
    """
    for req in requirements:
        req_tags = {str(t).lower() for t in getattr(req, "tags", [])}
        if req_tags & _CODING_KEYWORDS:
            return True
        raw_label = (getattr(req, "raw_label", "") or "").lower()
        if any(kw in raw_label for kw in ("coding", "algorithm", "data structure", "live coding")):
            return True

    concept_tags = {str(t).lower() for t in getattr(concept, "tags", [])}
    if concept_tags & _CODING_KEYWORDS:
        return True

    text_corpus = f"{concept.concept_id} {concept.label}".lower()
    return any(kw in text_corpus for kw in ("coding", "algorithm", "data_structure", "live_coding"))


def _derive_competency_plan_dynamic(
    *,
    job: CanonicalJob,
    match: MatchResult,
    duration_minutes: int,
    config: PlannerPolicyConfig,
) -> dict[str, Any]:
    """Gate 2 dynamic time envelope allocation algorithm (interview-planner-v2-dynamic)."""
    result_by_id = {item.requirement_id: item for item in match.requirement_results}
    non_competency_requirement_ids: list[str] = []
    had_taxonomy_concepts = False

    # 1. Map all canonical concepts and requirements (C_all partition tracking)
    canonical_map: dict[tuple[str, str], dict[str, Any]] = {}

    for requirement in job.requirements:
        concepts = _requirement_concepts(requirement)
        if not concepts:
            non_competency_requirement_ids.append(requirement.requirement_id)
            continue
        had_taxonomy_concepts = True

        result = result_by_id.get(requirement.requirement_id)
        priority_weight = _PRIORITY_WEIGHT.get(requirement.priority, 0.0)
        concept_count = len(concepts)

        for concept in concepts:
            match_status = _status_for_concept(result, concept.concept_id)
            status_boost = _STATUS_BOOST.get(match_status, 1.0)
            per_concept_weight = (priority_weight * status_boost / concept_count) if status_boost > 0.0 else 0.0

            key = (concept.taxonomy_version, concept.concept_id)
            if key not in canonical_map:
                canonical_map[key] = {
                    "concept": concept,
                    "requirements": [],
                    "requirement_ids": [],
                    "priorities": [],
                    "match_statuses": [],
                    "source_evidence_refs": [],
                    "raw_weight": 0.0,
                    "source": "job_requirement",
                }
            entry = canonical_map[key]
            entry["requirements"].append(requirement)
            entry["raw_weight"] += per_concept_weight
            if requirement.requirement_id not in entry["requirement_ids"]:
                entry["requirement_ids"].append(requirement.requirement_id)
            if requirement.priority not in entry["priorities"]:
                entry["priorities"].append(requirement.priority)
            if match_status not in entry["match_statuses"]:
                entry["match_statuses"].append(match_status)
            if requirement.source_evidence_ref not in entry["source_evidence_refs"]:
                entry["source_evidence_refs"].append(requirement.source_evidence_ref)

    if not canonical_map and not had_taxonomy_concepts:
        classifications = sorted(
            job.career_classifications,
            key=lambda item: (not item.is_primary, -item.confidence, item.code),
        )
        for classification in classifications[:2]:
            concept = TaxonomyRef(
                conceptId=classification.code,
                scheme="career",
                taxonomyVersion=classification.taxonomy_version,
                label=classification.label,
            )
            key = (concept.taxonomy_version, concept.concept_id)
            canonical_map[key] = {
                "concept": concept,
                "requirements": [],
                "requirement_ids": [],
                "priorities": ["must_have" if classification.is_primary else "nice_to_have"],
                "match_statuses": ["unknown"],
                "source_evidence_refs": [],
                "raw_weight": 1.0 if classification.is_primary else 0.5,
                "source": "career_classification_fallback",
            }

    if not canonical_map:
        raise ValueError("No applicable taxonomy-backed interview competencies could be derived from the job")

    # 2. Compute T_tech_pool
    total_session_seconds = duration_minutes * 60
    t_onboarding = config.t_onboarding_base + config.t_cv_addon_inclusive
    t_probe_pool = round(config.probe_pool_ratio * total_session_seconds)
    total_reserves = (
        t_onboarding
        + config.t_cv_standalone_reserve
        + config.t_behavioral
        + t_probe_pool
        + config.t_closing_reserve
    )
    if total_reserves > total_session_seconds:
        raise ValueError(
            f"Invalid budget configuration: total reserves ({total_reserves}s) "
            f"exceed session duration ({total_session_seconds}s)."
        )
    tech_pool_seconds = total_session_seconds - total_reserves

    # 3. Step 0: Pre-filtering 100% not_applicable targets
    candidates: list[dict[str, Any]] = []
    non_interviewed_targets: list[dict[str, Any]] = []

    for key, entry in canonical_map.items():
        concept = entry["concept"]
        reqs = entry["requirements"]
        match_statuses = entry["match_statuses"]
        is_all_na = bool(match_statuses) and all(s == "not_applicable" for s in match_statuses)

        is_coding = _is_coding_target(concept, reqs)
        preliminary_archetype = "CODING" if is_coding else "TEXT"
        entry["archetype"] = preliminary_archetype

        highest_priority = (
            "must_have"
            if "must_have" in entry["priorities"]
            else ("nice_to_have" if "nice_to_have" in entry["priorities"] else "context")
        )
        entry["priority"] = highest_priority

        if is_all_na:
            non_interviewed_targets.append({
                "conceptId": concept.concept_id,
                "taxonomyVersion": concept.taxonomy_version,
                "label": concept.label,
                "priority": highest_priority,
                "targetArchetype": preliminary_archetype,
                "omissionReason": "not_applicable_for_candidate",
                "omissionDetail": {
                    "matchStatus": "not_applicable",
                    "weight": 0.0,
                    "reason": "100% of underlying requirements evaluated as not_applicable",
                },
            })
        else:
            candidates.append(entry)

    evaluation_targets = build_evaluation_targets(job=job, match=match)
    validate_must_have_coverage(job=job, evaluation_targets=evaluation_targets)

    # 4. Handle Case: 100% of targets are not_applicable (Option B)
    if not candidates:
        plan = {
            "policyVersion": PLANNER_POLICY_VERSION_DYNAMIC,
            "sessionDurationMinutes": duration_minutes,
            "sessionDurationSeconds": total_session_seconds,
            "onboardingReserveSeconds": t_onboarding,
            "cvStandaloneReserveSeconds": config.t_cv_standalone_reserve,
            "behavioralReserveSeconds": config.t_behavioral,
            "probePoolSeconds": t_probe_pool,
            "closingReserveSeconds": config.t_closing_reserve,
            "techPoolSeconds": tech_pool_seconds,
            "unallocatedBufferSeconds": tech_pool_seconds,
            "difficulty": derive_difficulty(job),
            "sections": build_sections(duration_minutes),
            "targets": [],
            "nonInterviewedTargets": non_interviewed_targets,
            "evaluationTargets": evaluation_targets,
            "nonCompetencyRequirementIds": non_competency_requirement_ids,
            "estimatedTurnsRange": {
                "estimatedFrozenTurnsRange": [
                    config.n_onboarding + (1 if config.t_behavioral > 0 else 0),
                    config.n_onboarding + (1 if config.t_behavioral > 0 else 0),
                ],
                "estimatedTotalInteractionTurnsRange": [
                    config.n_onboarding + (1 if config.t_behavioral > 0 else 0),
                    None,
                ],
            },
            "candidateMatrix": {
                "strengthsToVerify": [],
                "gapCompetencies": [],
            },
        }
        canonical = json.dumps(plan, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        plan["fingerprint"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return plan

    # 5. Deterministic Ranking of Candidate Targets
    ranked_candidates = sorted(
        candidates,
        key=lambda item: (
            0 if "must_have" in item["priorities"] else 1,
            -item["raw_weight"],
            item["concept"].taxonomy_version,
            item["concept"].concept_id,
        ),
    )

    # 6. Step 1: Sequential Allocation & Policy Decisions (Decisions 5 & 7)
    eligible_targets: list[dict[str, Any]] = []
    accumulated_floor = 0
    precedence_hands_on = resolve_strict_hands_on_precedence(job=job, config=config)

    for i, item in enumerate(ranked_candidates):
        rem = tech_pool_seconds - accumulated_floor
        arch = item["archetype"]
        remaining_candidates = ranked_candidates[i + 1:]

        if arch == "TEXT":
            floor = config.t_arch_text_min
            if accumulated_floor + floor <= tech_pool_seconds:
                item["targetArchetype"] = "TEXT"
                item["floorSeconds"] = floor
                eligible_targets.append(item)
                accumulated_floor += floor
            else:
                non_interviewed_targets.append({
                    "conceptId": item["concept"].concept_id,
                    "taxonomyVersion": item["concept"].taxonomy_version,
                    "label": item["concept"].label,
                    "priority": item["priority"],
                    "targetArchetype": "TEXT",
                    "omissionReason": "insufficient_tech_pool_for_minimum_envelope",
                    "omissionDetail": {
                        "poolRemainingSeconds": rem,
                        "floorRequiredSeconds": floor,
                    },
                })
                if config.strict_priority_stop:
                    for rem_t in remaining_candidates:
                        non_interviewed_targets.append({
                            "conceptId": rem_t["concept"].concept_id,
                            "taxonomyVersion": rem_t["concept"].taxonomy_version,
                            "label": rem_t["concept"].label,
                            "priority": rem_t["priority"],
                            "targetArchetype": rem_t["archetype"],
                            "omissionReason": "strict_priority_halted_due_to_higher_rank",
                            "omissionDetail": {
                                "haltedByConceptId": item["concept"].concept_id,
                                "reason": "Strict priority stop halted due to higher rank target insufficiency",
                            },
                        })
                    break
                else:
                    continue

        elif arch == "CODING":
            coding_floor = config.t_arch_code_min
            if rem >= coding_floor:
                item["targetArchetype"] = "CODING"
                item["floorSeconds"] = coding_floor
                eligible_targets.append(item)
                accumulated_floor += coding_floor
            else:
                # Decision 5 Coding Fallback applies BEFORE deciding eligibility
                # Precedence: True or None (missing flag -> fail-safe omission) prevents downgrade
                if precedence_hands_on is True or precedence_hands_on is None:
                    non_interviewed_targets.append({
                        "conceptId": item["concept"].concept_id,
                        "taxonomyVersion": item["concept"].taxonomy_version,
                        "label": item["concept"].label,
                        "priority": item["priority"],
                        "targetArchetype": "CODING",
                        "omissionReason": "insufficient_envelope_for_coding_assessment",
                        "omissionDetail": {
                            "poolRemainingSeconds": rem,
                            "floorRequiredSeconds": coding_floor,
                            "strictHandsOnRequired": precedence_hands_on,
                        },
                    })
                    if config.strict_priority_stop:
                        for rem_t in remaining_candidates:
                            non_interviewed_targets.append({
                                "conceptId": rem_t["concept"].concept_id,
                                "taxonomyVersion": rem_t["concept"].taxonomy_version,
                                "label": rem_t["concept"].label,
                                "priority": rem_t["priority"],
                                "targetArchetype": rem_t["archetype"],
                                "omissionReason": "strict_priority_halted_due_to_higher_rank",
                                "omissionDetail": {
                                    "haltedByConceptId": item["concept"].concept_id,
                                    "reason": "Strict priority stop halted due to higher rank target insufficiency",
                                },
                            })
                        break
                    else:
                        continue
                else:
                    # precedence_hands_on is False: adhere to coding_fallback_policy
                    if config.coding_fallback_policy == "downgrade_to_text":
                        text_floor = config.t_arch_text_min
                        if accumulated_floor + text_floor <= tech_pool_seconds:
                            item["targetArchetype"] = "TEXT"
                            item["floorSeconds"] = text_floor
                            eligible_targets.append(item)
                            accumulated_floor += text_floor
                        else:
                            non_interviewed_targets.append({
                                "conceptId": item["concept"].concept_id,
                                "taxonomyVersion": item["concept"].taxonomy_version,
                                "label": item["concept"].label,
                                "priority": item["priority"],
                                "targetArchetype": "TEXT",
                                "omissionReason": "insufficient_tech_pool_for_minimum_envelope",
                                "omissionDetail": {
                                    "poolRemainingSeconds": rem,
                                    "floorRequiredSeconds": text_floor,
                                },
                            })
                            if config.strict_priority_stop:
                                for rem_t in remaining_candidates:
                                    non_interviewed_targets.append({
                                        "conceptId": rem_t["concept"].concept_id,
                                        "taxonomyVersion": rem_t["concept"].taxonomy_version,
                                        "label": rem_t["concept"].label,
                                        "priority": rem_t["priority"],
                                        "targetArchetype": rem_t["archetype"],
                                        "omissionReason": "strict_priority_halted_due_to_higher_rank",
                                        "omissionDetail": {
                                            "haltedByConceptId": item["concept"].concept_id,
                                            "reason": "Strict priority stop halted due to higher rank target insufficiency",
                                        },
                                    })
                                break
                            else:
                                continue
                    else:
                        # 5B: omit
                        non_interviewed_targets.append({
                            "conceptId": item["concept"].concept_id,
                            "taxonomyVersion": item["concept"].taxonomy_version,
                            "label": item["concept"].label,
                            "priority": item["priority"],
                            "targetArchetype": "CODING",
                            "omissionReason": "insufficient_envelope_for_coding_assessment",
                            "omissionDetail": {
                                "poolRemainingSeconds": rem,
                                "floorRequiredSeconds": coding_floor,
                            },
                        })
                        if config.strict_priority_stop:
                            for rem_t in remaining_candidates:
                                non_interviewed_targets.append({
                                    "conceptId": rem_t["concept"].concept_id,
                                    "taxonomyVersion": rem_t["concept"].taxonomy_version,
                                    "label": rem_t["concept"].label,
                                    "priority": rem_t["priority"],
                                    "targetArchetype": rem_t["archetype"],
                                    "omissionReason": "strict_priority_halted_due_to_higher_rank",
                                    "omissionDetail": {
                                        "haltedByConceptId": item["concept"].concept_id,
                                        "reason": "Strict priority stop halted due to higher rank target insufficiency",
                                    },
                                })
                            break
                        else:
                            continue

    # 7. Step 2: Surplus Allocation via Hamilton-Hare on Integer Seconds
    targets: list[dict[str, Any]] = []
    if not eligible_targets:
        unallocated_buffer_seconds = tech_pool_seconds
    else:
        surplus = tech_pool_seconds - accumulated_floor
        total_w = sum(t["raw_weight"] for t in eligible_targets)
        if total_w == 0.0:
            # Defensive Zero-Weight Fallback: Equal share allocation without ZeroDivisionError
            normalized_w = [1.0 / len(eligible_targets)] * len(eligible_targets)
        else:
            normalized_w = [t["raw_weight"] / total_w for t in eligible_targets]

        ideal_surplus = [surplus * w for w in normalized_w]
        floor_surplus = [int(math.floor(val)) for val in ideal_surplus]
        fractions = [ideal_surplus[i] - floor_surplus[i] for i in range(len(eligible_targets))]
        delta = surplus - sum(floor_surplus)

        sorted_indices = sorted(
            range(len(eligible_targets)),
            key=lambda idx: (-fractions[idx], idx),
        )
        bonus = [0] * len(eligible_targets)
        for idx in sorted_indices[:delta]:
            bonus[idx] += 1

        for rank, (item, norm_w, fl_s, b) in enumerate(zip(eligible_targets, normalized_w, floor_surplus, bonus)):
            envelope = item["floorSeconds"] + fl_s + b
            arch = item["targetArchetype"]
            if arch == "CODING":
                min_q = max(1, envelope // config.t_arch_code_max)
                max_q = max(1, math.ceil(envelope / config.t_arch_code_min))
            else:
                min_q = max(1, envelope // config.t_arch_text_max)
                max_q = max(1, math.ceil(envelope / config.t_arch_text_min))

            targets.append({
                "selectionRank": rank,
                "taxonomyVersion": item["concept"].taxonomy_version,
                "conceptId": item["concept"].concept_id,
                "label": item["concept"].label,
                "targetArchetype": arch,
                "timeEnvelopeSeconds": envelope,
                "floorSeconds": item["floorSeconds"],
                "estimatedQuestionsRange": [min_q, max_q],
                "importance": round(norm_w, 6),
                "targetQuestionCount": max(1, min_q),
                "rationale": {
                    "source": item["source"],
                    "requirementIds": item["requirement_ids"],
                    "priorities": item["priorities"],
                    "matchStatuses": item["match_statuses"],
                    "jobEvidenceRefs": item["source_evidence_refs"],
                },
            })
        unallocated_buffer_seconds = 0

    # Verification of Invariants
    total_env = sum(t["timeEnvelopeSeconds"] for t in targets)
    assert total_env + unallocated_buffer_seconds == tech_pool_seconds, "Invariant 1 & 2 Conservation Violation"
    assert len(canonical_map) == len(targets) + len(non_interviewed_targets), "Invariant 4B Partition Violation"

    strengths_to_verify = [
        t["label"] or t["conceptId"]
        for t in targets
        if "met" in (t.get("rationale", {}).get("matchStatuses") or [])
    ]
    gap_competencies = [
        t["label"] or t["conceptId"]
        for t in targets
        if any(s in ("not_met", "unknown") for s in (t.get("rationale", {}).get("matchStatuses") or []))
    ]

    min_tech = sum(t["estimatedQuestionsRange"][0] for t in targets)
    max_tech = sum(t["estimatedQuestionsRange"][1] for t in targets)
    star_turns = 1 if config.t_behavioral > 0 else 0
    min_frozen = config.n_onboarding + min_tech + star_turns
    max_frozen = config.n_onboarding + max_tech + star_turns

    plan = {
        "policyVersion": PLANNER_POLICY_VERSION_DYNAMIC,
        "sessionDurationMinutes": duration_minutes,
        "sessionDurationSeconds": total_session_seconds,
        "onboardingReserveSeconds": t_onboarding,
        "cvStandaloneReserveSeconds": config.t_cv_standalone_reserve,
        "behavioralReserveSeconds": config.t_behavioral,
        "probePoolSeconds": t_probe_pool,
        "closingReserveSeconds": config.t_closing_reserve,
        "techPoolSeconds": tech_pool_seconds,
        "unallocatedBufferSeconds": unallocated_buffer_seconds,
        "difficulty": derive_difficulty(job),
        "sections": build_sections(duration_minutes),
        "targets": targets,
        "nonInterviewedTargets": non_interviewed_targets,
        "evaluationTargets": evaluation_targets,
        "nonCompetencyRequirementIds": non_competency_requirement_ids,
        "estimatedTurnsRange": {
            "estimatedFrozenTurnsRange": [min_frozen, max_frozen],
            "estimatedTotalInteractionTurnsRange": [min_frozen, None],
        },
        "candidateMatrix": {
            "strengthsToVerify": strengths_to_verify,
            "gapCompetencies": gap_competencies,
        },
    }
    canonical = json.dumps(plan, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    plan["fingerprint"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return plan


def derive_competency_plan(
    *,
    job: CanonicalJob,
    match: MatchResult,
    duration_minutes: int,
    policy_config: PlannerPolicyConfig | None = None,
) -> dict[str, Any]:
    """Build a deterministic competency agenda from canonical requirements.

    When `policy_config` is None, executes the strict legacy allocation algorithm
    (policyVersion "interview-planner-v1") to preserve full backward compatibility
    for existing callers without imposing unapproved Product assumptions.

    When `policy_config` is explicitly provided, executes the Gate 2 dynamic time
    envelope allocation algorithm (policyVersion "interview-planner-v2-dynamic").
    """
    if policy_config is None:
        return _derive_competency_plan_legacy(
            job=job,
            match=match,
            duration_minutes=duration_minutes,
        )
    return _derive_competency_plan_dynamic(
        job=job,
        match=match,
        duration_minutes=duration_minutes,
        config=policy_config,
    )


async def _load_resume(
    db: AsyncSession,
    *,
    user_id: str,
    resume_id: str,
) -> tuple[CanonicalResume, str | None]:
    result = await db.execute(
        text(
            "SELECT parsed_data, raw_text, checksum FROM user_cvs "
            "WHERE id = :id AND user_id = :uid"
        ),
        {"id": resume_id, "uid": user_id},
    )
    row = result.mappings().one_or_none()
    if row is None:
        raise ValueError("CV not found")
    if not row["parsed_data"]:
        raise ValueError("CV canonical parsing is incomplete")
    try:
        resume = CanonicalResume.model_validate(row["parsed_data"]).model_copy(
            update={"raw_text": row["raw_text"] or ""}
        )
    except ValidationError as exc:
        raise ValueError("CV canonical data is invalid") from exc
    return resume, row["checksum"]


async def _load_job(
    db: AsyncSession,
    *,
    user: dict,
    job_id: str,
) -> CanonicalJob:
    filters = ["id = :id", "item_type = 'JOB_DESCRIPTION'"]
    if "ADMIN" not in user.get("roles", []):
        filters.append("listing_status = 'ACTIVE'")
    result = await db.execute(
        text(
            "SELECT structured_data, active_version_id FROM job_descriptions WHERE "
            + " AND ".join(filters)
        ),
        {"id": job_id},
    )
    row = result.mappings().one_or_none()
    if row is None:
        raise ValueError("Job description not found")
    if not row["structured_data"]:
        raise ValueError("Job canonical parsing is incomplete")
    try:
        parsed = CanonicalJobDescription.model_validate(row["structured_data"])
        job = canonical_job_from_description(
            parsed,
            job_id=job_id,
            job_version_id=(str(row["active_version_id"]) if row["active_version_id"] else None),
        )
    except (ValidationError, ValueError) as exc:
        raise ValueError("Job canonical data is invalid") from exc
    return job


async def build_and_persist_session_plan(
    *,
    db: AsyncSession,
    user: dict,
    session_row: dict,
) -> dict[str, Any]:
    plan_id = session_row.get("plan_id")
    if not plan_id:
        raise ValueError("Interview session has no plan container")
    if session_row.get("plan_status") == "LOCKED":
        raise RuntimeError("Interview plan is already locked")

    resume, resume_checksum = await _load_resume(
        db,
        user_id=user["sub"],
        resume_id=session_row["resume_id"],
    )
    job = await _load_job(db, user=user, job_id=session_row["job_id"])

    match = await evaluate_match(
        MatchRequest(
            schemaVersion="2.1",
            resume=resume,
            job=job,
            asyncProcessing=False,
        )
    )
    plan = derive_competency_plan(
        job=job,
        match=match,
        duration_minutes=session_row["duration_minutes"],
    )

    # Re-acquire the plan row under a write lock immediately before
    # persistence. Matching can take time, so the session snapshot received by
    # the router may be stale by now (for example P2 could have LOCKED the plan).
    # Serializing only the persistence phase keeps rebuilds idempotent without
    # holding a database lock during matching/provider work.
    locked_plan_result = await db.execute(
        text(
            "SELECT status, source_context FROM interview_session_plans "
            "WHERE id = :id AND session_id = :session_id FOR UPDATE"
        ),
        {"id": plan_id, "session_id": session_row["id"]},
    )
    locked_plan = locked_plan_result.mappings().one_or_none()
    if locked_plan is None:
        raise ValueError("Interview plan not found")
    if locked_plan["status"] == "LOCKED":
        raise RuntimeError("Interview plan is already locked")

    existing_context = locked_plan["source_context"] or {}
    source_context = {
        **existing_context,
        "resumeId": session_row["resume_id"],
        "resumeChecksum": resume_checksum,
        "jobId": session_row["job_id"],
        "jobVersionId": job.job_version_id,
        "jobDocumentSha256": job.document_sha256,
        "matchingPipelineVersion": match.pipeline_version,
        "matchingPolicyVersion": match.policy_version,
        "matchingDecision": match.decision,
        "eligibility": match.eligibility,
        "fitBand": match.fit_band,
        "plannerPolicyVersion": plan["policyVersion"],
        "questionBudget": plan["questionBudget"],
        "nonCompetencyRequirementIds": plan["nonCompetencyRequirementIds"],
    }

    await db.execute(
        text("DELETE FROM session_competency_targets WHERE plan_id = :plan_id"),
        {"plan_id": plan_id},
    )
    for target in plan["targets"]:
        await db.execute(
            text(
                "INSERT INTO session_competency_targets "
                "(id, plan_id, selection_rank, taxonomy_version, concept_id, label, "
                "importance, target_question_count, rationale) "
                "VALUES (:id, :plan_id, :selection_rank, :taxonomy_version, :concept_id, "
                ":label, :importance, :question_count, CAST(:rationale AS jsonb))"
            ),
            {
                "id": str(uuid4()),
                "plan_id": plan_id,
                "selection_rank": target["selectionRank"],
                "taxonomy_version": target["taxonomyVersion"],
                "concept_id": target["conceptId"],
                "label": target["label"],
                "importance": target["importance"],
                "question_count": target["targetQuestionCount"],
                "rationale": json.dumps(target["rationale"]),
            },
        )

    await db.execute(
        text(
            "UPDATE interview_session_plans "
            "SET status = 'READY', source_context = CAST(:context AS jsonb), "
            "plan_payload = CAST(:plan_payload AS jsonb), updated_at = now() "
            "WHERE id = :id"
        ),
        {
            "id": plan_id,
            "context": json.dumps(source_context),
            "plan_payload": json.dumps(
                {
                    "policyVersion": plan["policyVersion"],
                    "fingerprint": plan["fingerprint"],
                    "questionBudget": plan["questionBudget"],
                    "difficulty": plan["difficulty"],
                    "sections": plan["sections"],
                    "evaluationTargets": plan["evaluationTargets"],
                }
            ),
        },
    )
    await db.commit()

    return await read_session_plan(db=db, plan_id=plan_id, session_id=session_row["id"])


async def read_session_plan(
    *,
    db: AsyncSession,
    plan_id: str,
    session_id: str,
) -> dict[str, Any]:
    plan_result = await db.execute(
        text(
            "SELECT id, schema_version, status, source_context, plan_payload, created_at, updated_at "
            "FROM interview_session_plans WHERE id = :id AND session_id = :session_id"
        ),
        {"id": plan_id, "session_id": session_id},
    )
    plan_row = plan_result.mappings().one_or_none()
    if plan_row is None:
        raise ValueError("Interview plan not found")

    targets_result = await db.execute(
        text(
            "SELECT selection_rank, taxonomy_version, concept_id, label, importance, "
            "target_question_count, rationale "
            "FROM session_competency_targets WHERE plan_id = :plan_id "
            "ORDER BY selection_rank NULLS LAST, importance DESC, taxonomy_version, concept_id"
        ),
        {"plan_id": plan_id},
    )
    targets = [
        {
            "selectionRank": row["selection_rank"],
            "taxonomyVersion": row["taxonomy_version"],
            "conceptId": row["concept_id"],
            "label": row["label"],
            "importance": float(row["importance"]),
            "targetQuestionCount": row["target_question_count"],
            "rationale": row["rationale"],
        }
        for row in targets_result.mappings().all()
    ]
    context = plan_row["source_context"] or {}
    payload = plan_row["plan_payload"] or {}
    return {
        "planId": plan_row["id"],
        "sessionId": session_id,
        "schemaVersion": plan_row["schema_version"],
        "status": plan_row["status"],
        "policyVersion": payload.get("policyVersion") or context.get("plannerPolicyVersion"),
        "fingerprint": payload.get("fingerprint"),
        "questionBudget": payload.get("questionBudget") or context.get("questionBudget"),
        "targetQuestionCount": sum(item["targetQuestionCount"] for item in targets),
        "difficulty": payload.get("difficulty"),
        "sections": payload.get("sections", []),
        "evaluationTargets": payload.get("evaluationTargets", []),
        "sourceContext": context,
        "targets": targets,
        "createdAt": plan_row["created_at"].isoformat(),
        "updatedAt": plan_row["updated_at"].isoformat(),
    }
