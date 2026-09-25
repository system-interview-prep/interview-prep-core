"""Deterministic P2 question selector for the structured interview runtime.

P2 consumes a READY P1 plan and freezes immutable Question Bank versions into
interview_turns. It never generates fallback questions.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

SELECTOR_POLICY_VERSION = "interview-question-selector-v1"
_ELIGIBLE_STATUSES = {"APPROVED", "CALIBRATED"}
_DIFFICULTY_ORDER = {
    "foundational": 0,
    "intermediate": 1,
    "advanced": 2,
}


class QuestionUnavailableError(ValueError):
    """Raised when the approved bank cannot satisfy the frozen P1 plan."""


@dataclass(frozen=True)
class _Candidate:
    question_version_id: str
    rubric_version_id: str
    stable_key: str
    version: str
    question_type: str
    difficulty_band: str
    canonical_locale: str
    question_text: str
    objective: str
    soft_answer_seconds: int
    hard_answer_seconds: int
    mapping_purpose: str
    relevance: float
    expected_points: list[dict[str, Any]]
    rubric: dict[str, Any]


def _allowed_purposes(target: dict[str, Any]) -> tuple[str, ...]:
    source = (target.get("rationale") or {}).get("source")
    if source == "career_classification_fallback":
        return ("TARGET_ROLE",)
    # P1 requirement targets are skills/atomic concepts. Prefer an explicit
    # skill mapping; PRIMARY_COMPETENCY remains eligible for banks that model
    # the same taxonomy concept directly as the assessed competency.
    return ("TARGET_SKILL", "PRIMARY_COMPETENCY")


def _difficulty_distance(candidate: str, requested: str) -> int:
    if requested == "unspecified":
        return 0
    left = _DIFFICULTY_ORDER.get(candidate)
    right = _DIFFICULTY_ORDER.get(requested)
    if left is None or right is None:
        return 99
    return abs(left - right)


def _locale_rank(candidate: str, requested: str) -> int:
    if candidate.lower() == requested.lower():
        return 0
    if candidate.split("-", 1)[0].lower() == requested.split("-", 1)[0].lower():
        return 1
    return 99


def _candidate_rank(candidate: _Candidate, *, difficulty: str, locale: str) -> tuple[Any, ...]:
    purpose_rank = {
        "PRIMARY_COMPETENCY": 0,
        "TARGET_SKILL": 1,
        "TARGET_ROLE": 2,
    }.get(candidate.mapping_purpose, 9)
    return (
        _locale_rank(candidate.canonical_locale, locale),
        _difficulty_distance(candidate.difficulty_band, difficulty),
        purpose_rank,
        -candidate.relevance,
        candidate.stable_key,
        candidate.version,
        candidate.question_version_id,
    )


async def _load_candidates(
    db: AsyncSession,
    *,
    target: dict[str, Any],
    locale: str,
    difficulty: str,
) -> list[_Candidate]:
    purposes = _allowed_purposes(target)
    result = await db.execute(
        text(
            """
            SELECT q.stable_key, qv.id AS question_version_id, qv.version,
                   qv.status, qv.question_type, qv.difficulty_band,
                   qv.canonical_locale, qv.canonical_text, qv.objective,
                   qv.soft_answer_seconds, qv.hard_answer_seconds,
                   map.purpose AS mapping_purpose, map.relevance,
                   qvr.rubric_version_id
            FROM interview_questions q
            JOIN interview_question_versions qv
              ON qv.id = q.current_approved_version_id
            JOIN question_version_taxonomy_concepts map
              ON map.question_version_id = qv.id
            JOIN question_version_rubrics qvr
              ON qvr.question_version_id = qv.id
            WHERE q.retired_at IS NULL
              AND qv.status IN ('APPROVED', 'CALIBRATED')
              AND qv.taxonomy_version = :taxonomy_version
              AND map.taxonomy_version = :taxonomy_version
              AND map.concept_id = :concept_id
              AND map.purpose = ANY(:purposes)
            """
        ),
        {
            "taxonomy_version": target["taxonomyVersion"],
            "concept_id": target["conceptId"],
            "purposes": list(purposes),
        },
    )

    candidates: list[_Candidate] = []
    for row in result.mappings().all():
        if row["status"] not in _ELIGIBLE_STATUSES:
            continue
        if _locale_rank(row["canonical_locale"], locale) >= 99:
            continue
        if difficulty != "unspecified" and _difficulty_distance(row["difficulty_band"], difficulty) >= 99:
            continue

        expected_result = await db.execute(
            text(
                """
                SELECT stable_key, description, importance, display_order
                FROM expected_points
                WHERE question_version_id = :question_version_id
                ORDER BY display_order, stable_key
                """
            ),
            {"question_version_id": row["question_version_id"]},
        )
        expected_points = [dict(item) for item in expected_result.mappings().all()]

        rubric_result = await db.execute(
            text(
                """
                SELECT id, version, score_min, score_max, minimum_coverage,
                       aggregation_method, aggregation_policy
                FROM rubric_versions
                WHERE id = :rubric_version_id
                """
            ),
            {"rubric_version_id": row["rubric_version_id"]},
        )
        rubric_row = rubric_result.mappings().one_or_none()
        if rubric_row is None:
            continue

        criteria_result = await db.execute(
            text(
                """
                SELECT id, stable_key, name, description, weight, critical, display_order
                FROM rubric_criteria
                WHERE rubric_version_id = :rubric_version_id
                ORDER BY display_order, stable_key
                """
            ),
            {"rubric_version_id": row["rubric_version_id"]},
        )
        criteria = []
        for criterion in criteria_result.mappings().all():
            anchors_result = await db.execute(
                text(
                    """
                    SELECT level, description, positive_indicators, negative_indicators
                    FROM rubric_anchors
                    WHERE criterion_id = :criterion_id
                    ORDER BY level
                    """
                ),
                {"criterion_id": criterion["id"]},
            )
            criteria.append(
                {
                    "criterionId": str(criterion["id"]),
                    "stableKey": criterion["stable_key"],
                    "name": criterion["name"],
                    "description": criterion["description"],
                    "weight": float(criterion["weight"]),
                    "critical": criterion["critical"],
                    "anchors": [dict(anchor) for anchor in anchors_result.mappings().all()],
                }
            )

        candidates.append(
            _Candidate(
                question_version_id=str(row["question_version_id"]),
                rubric_version_id=str(row["rubric_version_id"]),
                stable_key=row["stable_key"],
                version=row["version"],
                question_type=row["question_type"],
                difficulty_band=row["difficulty_band"],
                canonical_locale=row["canonical_locale"],
                question_text=row["canonical_text"],
                objective=row["objective"],
                soft_answer_seconds=row["soft_answer_seconds"],
                hard_answer_seconds=row["hard_answer_seconds"],
                mapping_purpose=row["mapping_purpose"],
                relevance=float(row["relevance"]),
                expected_points=expected_points,
                rubric={
                    "rubricVersionId": str(rubric_row["id"]),
                    "version": rubric_row["version"],
                    "scoreMin": rubric_row["score_min"],
                    "scoreMax": rubric_row["score_max"],
                    "minimumCoverage": float(rubric_row["minimum_coverage"]),
                    "aggregationMethod": rubric_row["aggregation_method"],
                    "aggregationPolicy": rubric_row["aggregation_policy"] or {},
                    "criteria": criteria,
                },
            )
        )
    return sorted(
        candidates,
        key=lambda item: _candidate_rank(item, difficulty=difficulty, locale=locale),
    )


def _snapshot(
    candidate: _Candidate,
    *,
    target: dict[str, Any],
    locale: str,
    selection_rank: int,
) -> dict[str, Any]:
    return {
        "schemaVersion": "1.0",
        "selectorPolicyVersion": SELECTOR_POLICY_VERSION,
        "questionVersionId": candidate.question_version_id,
        "stableKey": candidate.stable_key,
        "version": candidate.version,
        "questionType": candidate.question_type,
        "difficulty": candidate.difficulty_band,
        "locale": locale,
        "canonicalLocale": candidate.canonical_locale,
        "questionText": candidate.question_text,
        "objective": candidate.objective,
        "softAnswerSeconds": candidate.soft_answer_seconds,
        "hardAnswerSeconds": candidate.hard_answer_seconds,
        "taxonomyTarget": {
            "taxonomyVersion": target["taxonomyVersion"],
            "conceptId": target["conceptId"],
            "label": target["label"],
            "mappingPurpose": candidate.mapping_purpose,
            "relevance": candidate.relevance,
        },
        "selectionRank": selection_rank,
        "expectedPoints": candidate.expected_points,
        "rubric": candidate.rubric,
    }


async def select_and_freeze_questions(
    *,
    db: AsyncSession,
    session_row: dict[str, Any],
) -> dict[str, Any]:
    """Freeze deterministic Question Bank selections and lock the P1 plan."""

    plan_id = session_row.get("plan_id")
    if not plan_id:
        raise ValueError("Interview session has no plan container")

    plan_result = await db.execute(
        text(
            """
            SELECT status, plan_payload
            FROM interview_session_plans
            WHERE id = :plan_id AND session_id = :session_id
            FOR UPDATE
            """
        ),
        {"plan_id": plan_id, "session_id": session_row["id"]},
    )
    plan = plan_result.mappings().one_or_none()
    if plan is None:
        raise ValueError("Interview plan not found")
    if plan["status"] == "LOCKED":
        turns = await read_frozen_turns(db=db, session_id=session_row["id"])
        return {
            "sessionId": session_row["id"],
            "planId": plan_id,
            "status": "LOCKED",
            "selectorPolicyVersion": SELECTOR_POLICY_VERSION,
            "turns": turns,
        }
    if plan["status"] != "READY":
        raise RuntimeError("Interview plan must be READY before question selection")

    targets_result = await db.execute(
        text(
            """
            SELECT selection_rank, taxonomy_version, concept_id, label,
                   importance, target_question_count, rationale
            FROM session_competency_targets
            WHERE plan_id = :plan_id
            ORDER BY selection_rank NULLS LAST, importance DESC,
                     taxonomy_version, concept_id
            """
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
            "rationale": row["rationale"] or {},
        }
        for row in targets_result.mappings().all()
    ]
    if not targets:
        raise QuestionUnavailableError("question_unavailable: interview plan has no competency targets")

    payload = plan["plan_payload"] or {}
    difficulty = (payload.get("difficulty") or {}).get("level", "unspecified")
    locale = session_row["locale"]
    used_versions: set[str] = set()
    frozen: list[tuple[_Candidate, dict[str, Any], int]] = []

    for target in targets:
        candidates = await _load_candidates(
            db,
            target=target,
            locale=locale,
            difficulty=difficulty,
        )
        available = [item for item in candidates if item.question_version_id not in used_versions]
        needed = target["targetQuestionCount"]
        if len(available) < needed:
            raise QuestionUnavailableError(
                "question_unavailable: "
                f"{target['taxonomyVersion']}:{target['conceptId']} requires {needed} "
                f"question(s), but only {len(available)} eligible approved/calibrated version(s) exist"
            )
        for candidate in available[:needed]:
            frozen.append((candidate, target, len(frozen)))
            used_versions.add(candidate.question_version_id)

    # Do not partially write turns before every target is satisfiable.
    await db.execute(
        text("DELETE FROM interview_turns WHERE session_id = :session_id"),
        {"session_id": session_row["id"]},
    )
    for candidate, target, turn_index in frozen:
        snapshot = _snapshot(
            candidate,
            target=target,
            locale=locale,
            selection_rank=turn_index,
        )
        await db.execute(
            text(
                """
                INSERT INTO interview_turns
                    (id, session_id, turn_index, status, question_version_id,
                     rubric_version_id, question_snapshot)
                VALUES
                    (:id, :session_id, :turn_index, 'PLANNED',
                     CAST(:question_version_id AS uuid),
                     CAST(:rubric_version_id AS uuid),
                     CAST(:snapshot AS jsonb))
                """
            ),
            {
                "id": str(uuid4()),
                "session_id": session_row["id"],
                "turn_index": turn_index,
                "question_version_id": candidate.question_version_id,
                "rubric_version_id": candidate.rubric_version_id,
                "snapshot": json.dumps(snapshot),
            },
        )

    selection_contract = [
        {
            "turnIndex": turn_index,
            "questionVersionId": candidate.question_version_id,
            "rubricVersionId": candidate.rubric_version_id,
            "target": f"{target['taxonomyVersion']}:{target['conceptId']}",
        }
        for candidate, target, turn_index in frozen
    ]
    fingerprint = hashlib.sha256(
        json.dumps(selection_contract, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    await db.execute(
        text(
            """
            UPDATE interview_session_plans
            SET status = 'LOCKED',
                plan_payload = plan_payload || CAST(:selection AS jsonb),
                updated_at = now()
            WHERE id = :plan_id AND status = 'READY'
            """
        ),
        {
            "plan_id": plan_id,
            "selection": json.dumps(
                {
                    "questionSelection": {
                        "policyVersion": SELECTOR_POLICY_VERSION,
                        "fingerprint": fingerprint,
                        "turnCount": len(frozen),
                    }
                }
            ),
        },
    )
    await db.commit()
    return {
        "sessionId": session_row["id"],
        "planId": plan_id,
        "status": "LOCKED",
        "selectorPolicyVersion": SELECTOR_POLICY_VERSION,
        "fingerprint": fingerprint,
        "turns": await read_frozen_turns(db=db, session_id=session_row["id"]),
    }


async def read_frozen_turns(*, db: AsyncSession, session_id: str) -> list[dict[str, Any]]:
    result = await db.execute(
        text(
            """
            SELECT id, turn_index, status, question_version_id,
                   rubric_version_id, question_snapshot
            FROM interview_turns
            WHERE session_id = :session_id
            ORDER BY turn_index
            """
        ),
        {"session_id": session_id},
    )
    return [
        {
            "turnId": row["id"],
            "turnIndex": row["turn_index"],
            "status": row["status"],
            "questionVersionId": str(row["question_version_id"]) if row["question_version_id"] else None,
            "rubricVersionId": str(row["rubric_version_id"]) if row["rubric_version_id"] else None,
            "question": row["question_snapshot"] or {},
        }
        for row in result.mappings().all()
    ]
