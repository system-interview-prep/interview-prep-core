"""Deterministic P1 interview planner.

P1 converts grounded CV/JD matching context into a persisted competency agenda.
It does not select questions and it does not ask an LLM to invent competencies.
"""

import hashlib
import json
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
    """Bound the P1 agenda without pretending each question has exact duration."""
    estimated = round(duration_minutes / 4)
    return max(3, min(8, estimated))


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


def derive_competency_plan(
    *,
    job: CanonicalJob,
    match: MatchResult,
    duration_minutes: int,
) -> dict[str, Any]:
    """Build a deterministic competency agenda from canonical requirements.

    Requirement priority is the primary signal. Matching status only boosts
    areas that need more validation/practice; a CV claim marked MET still
    remains interviewable and is never treated as proof that the interview can
    skip that competency.
    """
    result_by_id = {item.requirement_id: item for item in match.requirement_results}
    candidates: dict[tuple[str, str], _CandidateTarget] = {}
    skipped_requirement_ids: list[str] = []

    for requirement in job.requirements:
        concepts = _requirement_concepts(requirement)
        if not concepts:
            skipped_requirement_ids.append(requirement.requirement_id)
            continue

        result = result_by_id.get(requirement.requirement_id)
        priority_weight = _PRIORITY_WEIGHT.get(requirement.priority, 0.0)
        concept_count = len(concepts)

        for concept in concepts:
            match_status = _status_for_concept(result, concept.concept_id)
            status_boost = _STATUS_BOOST.get(match_status, 1.0)
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

    # A canonical role classification is a controlled fallback only when the JD
    # has no taxonomy-backed requirement concepts. It is explicitly marked so
    # P2 can treat it differently from requirement-level competencies.
    if not candidates:
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
        raise ValueError("No taxonomy-backed interview competencies could be derived from the job")

    budget = _question_budget(duration_minutes)
    ranked = sorted(
        candidates.values(),
        key=lambda item: (
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
    for item, importance, target_count in zip(selected, normalized_weights, counts, strict=True):
        targets.append(
            {
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

    plan = {
        "policyVersion": PLANNER_POLICY_VERSION,
        "questionBudget": budget,
        "targetQuestionCount": sum(item["targetQuestionCount"] for item in targets),
        "difficulty": derive_difficulty(job),
        "sections": build_sections(duration_minutes),
        "targets": targets,
        "evaluationTargets": evaluation_targets,
        "skippedRequirementIds": skipped_requirement_ids,
    }
    canonical = json.dumps(plan, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    plan["fingerprint"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return plan


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
        "skippedRequirementIds": plan["skippedRequirementIds"],
    }

    await db.execute(
        text("DELETE FROM session_competency_targets WHERE plan_id = :plan_id"),
        {"plan_id": plan_id},
    )
    for target in plan["targets"]:
        await db.execute(
            text(
                "INSERT INTO session_competency_targets "
                "(id, plan_id, taxonomy_version, concept_id, label, importance, "
                "target_question_count, rationale) "
                "VALUES (:id, :plan_id, :taxonomy_version, :concept_id, :label, "
                ":importance, :question_count, CAST(:rationale AS jsonb))"
            ),
            {
                "id": str(uuid4()),
                "plan_id": plan_id,
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
            "SELECT taxonomy_version, concept_id, label, importance, "
            "target_question_count, rationale "
            "FROM session_competency_targets WHERE plan_id = :plan_id "
            "ORDER BY importance DESC, taxonomy_version, concept_id"
        ),
        {"plan_id": plan_id},
    )
    targets = [
        {
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
