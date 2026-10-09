"""Deterministic P2 question selector for the structured interview runtime.

P2 consumes a READY P1 plan and freezes qualifying Question Bank versions into
interview_turns. A target without enough approved questions walks a fallback
ladder: an approved question on a broader skill, then a role question whose own
skill the JD also requires, then a generated draft (filed IN_REVIEW by an
unlocked preflight, see ``question_generation``). A target that is still short
is interviewed with what was found, or dropped, and reported in
``questionSelection.uncoveredTargets``. Selection fails closed with
``question_bank_insufficient`` only when no target can be covered at all.
Every turn snapshot records its ``questionSource``.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.trace_logging import trace_event
from src.modules.interviews.core.demo_mode import is_demo_duration
from src.modules.interviews.planning.project_evidence import (
    build_project_validation_question,
    extract_project_evidences,
    select_best_project,
)
from src.modules.question_bank.facade import GENERATED_QUESTION_AUTHOR

SELECTOR_POLICY_VERSION = "interview-question-selector-v2"
_ELIGIBLE_STATUSES = {"APPROVED", "CALIBRATED"}
# Best-ranked candidates considered per target when enumerating subsets.
_SUBSET_CANDIDATE_POOL = 12
_DIFFICULTY_ORDER = {
    "foundational": 0,
    "intermediate": 1,
    "advanced": 2,
}


class QuestionUnavailableError(ValueError):
    """Raised when the approved bank cannot satisfy the frozen P1 plan."""

    def __init__(
        self,
        message: str = "Question bank cannot satisfy interview plan requirements under fail-closed policy",
        *,
        error_code: str = "question_bank_insufficient",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.details = details or {}

    def to_payload(self) -> dict[str, Any]:
        return {
            "errorCode": self.error_code,
            "message": str(self),
            "details": self.details,
        }


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
    thinking_seconds: int = 0
    canonical_snapshot: dict[str, Any] = field(default_factory=dict)
    # question_bank | broader_skill | role | generated_unreviewed
    source: str = "question_bank"
    # Set when question_text is an approved localization in the session locale;
    # holds the canonical text's locale (the rubric stays canonical).
    localized_from: str | None = None


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
    if requested.lower().startswith("vi") and candidate.lower().startswith("en"):
        return 10
    if requested.lower().startswith("en") and candidate.lower().startswith("vi"):
        return 10
    return 99


def _candidate_rank(
    candidate: _Candidate,
    *,
    difficulty: str,
    locale: str,
    salt: str = "",
    exposure: Mapping[str, str] | None = None,
) -> tuple[Any, ...]:
    purpose_rank = {
        "PRIMARY_COMPETENCY": 0,
        "TARGET_SKILL": 1,
        "TARGET_ROLE": 2,
    }.get(candidate.mapping_purpose, 9)
    # Session-salted tiebreaker creates diversity across candidates/sessions
    # while guaranteeing strict reproducibility for the same session.
    tiebreaker = (
        hashlib.md5(f"{salt}:{candidate.question_version_id}".encode("utf-8")).hexdigest()
        if salt
        else candidate.stable_key
    )
    # Rotation: among equally suitable questions (same locale, difficulty and
    # purpose) the candidate has not been asked before goes first, then the one
    # asked longest ago. Relevance only orders questions within that.
    last_seen = (exposure or {}).get(candidate.question_version_id)
    exposure_rank = (0, "") if last_seen is None else (1, last_seen)
    asked_locale = locale if candidate.localized_from else candidate.canonical_locale
    return (
        _locale_rank(asked_locale, locale),
        _difficulty_distance(candidate.difficulty_band, difficulty),
        purpose_rank,
        exposure_rank,
        -candidate.relevance,
        tiebreaker,
        candidate.version,
        candidate.question_version_id,
    )


async def _load_candidates(
    db: AsyncSession,
    *,
    target: dict[str, Any],
    locale: str,
    difficulty: str,
    salt: str = "",
    generated: bool = False,
    exposure: Mapping[str, str] | None = None,
) -> list[_Candidate]:
    """Approved questions mapped to the target, or (``generated``) the
    machine-generated drafts still waiting for review."""
    purposes = _allowed_purposes(target)
    version_join = "qv.question_id = q.id" if generated else "qv.id = q.current_approved_version_id"
    status_filter = (
        "qv.status IN ('DRAFT', 'IN_REVIEW') AND qv.created_by = :generator"
        if generated
        else "qv.status IN ('APPROVED', 'CALIBRATED')"
    )
    result = await db.execute(
        text(
            f"""
            SELECT q.stable_key, qv.id AS question_version_id, qv.version,
                   qv.status, qv.question_type, qv.difficulty_band,
                   qv.canonical_locale, qv.canonical_text, qv.objective,
                   qv.thinking_seconds, qv.soft_answer_seconds, qv.hard_answer_seconds,
                   qv.canonical_snapshot,
                   map.purpose AS mapping_purpose, map.relevance,
                   qvr.rubric_version_id,
                   loc.question_text AS localized_text
            FROM interview_questions q
            JOIN interview_question_versions qv
              ON {version_join}
            JOIN question_version_taxonomy_concepts map
              ON map.question_version_id = qv.id
            JOIN question_version_rubrics qvr
              ON qvr.question_version_id = qv.id
            -- Approved wording in the session locale (e.g. a vi-VN interview
            -- asking an en-US question): same question and rubric, asked in
            -- the candidate's language.
            LEFT JOIN question_localizations loc
              ON loc.question_version_id = qv.id
             AND lower(loc.locale) = lower(:locale)
             AND loc.status = 'APPROVED'
            WHERE q.retired_at IS NULL
              AND {status_filter}
              -- Concept ids are stable across taxonomy versions (parsers stamp
              -- `internal-2026.1`, the bank is authored against the version
              -- that holds the concepts, admins can clone new versions), so
              -- match on the id and only prefer an exact version match.
              AND map.concept_id = :concept_id
              AND map.purpose = ANY(:purposes)
            ORDER BY qv.id,
                     CASE WHEN map.taxonomy_version = :taxonomy_version THEN 0 ELSE 1 END,
                     CASE map.purpose
                         WHEN 'PRIMARY_COMPETENCY' THEN 0
                         WHEN 'TARGET_SKILL' THEN 1
                         WHEN 'TARGET_ROLE' THEN 2
                         ELSE 9
                     END,
                     map.relevance DESC
            """
        ),
        {
            "taxonomy_version": target["taxonomyVersion"],
            "concept_id": target["conceptId"],
            "purposes": list(purposes),
            "generator": GENERATED_QUESTION_AUTHOR,
            "locale": locale,
        },
    )

    candidates: list[_Candidate] = []
    seen_version_ids: set[str] = set()
    for row in result.mappings().all():
        # One question version may carry several taxonomy mappings for the same
        # concept (for example PRIMARY_COMPETENCY and TARGET_SKILL), which the
        # JOIN returns as separate rows. The query groups rows per version and
        # orders purposes exactly as `_candidate_rank` does, so keeping the first
        # row picks the strongest mapping and a single question can never be
        # frozen twice into the same interview.
        version_id = str(row["question_version_id"])
        if version_id in seen_version_ids:
            continue
        if not generated and row["status"] not in _ELIGIBLE_STATUSES:
            continue
        localized = bool(row.get("localized_text")) and row["canonical_locale"].lower() != locale.lower()
        if _locale_rank(locale if localized else row["canonical_locale"], locale) >= 99:
            continue
        if difficulty != "unspecified" and _difficulty_distance(row["difficulty_band"], difficulty) >= 99:
            continue
        seen_version_ids.add(version_id)

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
                question_text=row["localized_text"] if localized else row["canonical_text"],
                objective=row["objective"],
                soft_answer_seconds=row["soft_answer_seconds"],
                hard_answer_seconds=row["hard_answer_seconds"],
                mapping_purpose=row["mapping_purpose"],
                relevance=float(row["relevance"]),
                expected_points=expected_points,
                thinking_seconds=int(row.get("thinking_seconds") or 0),
                canonical_snapshot=dict(row.get("canonical_snapshot") or {}),
                source="generated_unreviewed" if generated else "question_bank",
                localized_from=row["canonical_locale"] if localized else None,
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
        key=lambda item: _candidate_rank(
            item, difficulty=difficulty, locale=locale, salt=salt, exposure=exposure
        ),
    )


async def _question_exposure(db: AsyncSession, session_row: dict[str, Any]) -> dict[str, str]:
    """When the candidate was last asked each question, across their other sessions.

    A new session for the same CV and JD keeps the same competencies (the
    planner is deterministic) but rotates the concrete questions, so practice
    does not turn into memorising answers. Resuming a session never reselects:
    its turns are already frozen. Demo sessions keep fixed questions so the
    scripted demo answers stay valid.
    """
    if is_demo_duration(session_row.get("duration_minutes")):
        return {}
    result = await db.execute(
        text(
            """
            SELECT t.question_version_id::text AS question_version_id,
                   MAX(COALESCE(t.started_at, t.updated_at)) AS last_seen
            FROM interview_turns t
            JOIN interview_sessions s ON s.id = t.session_id
            WHERE s.user_id = (SELECT user_id FROM interview_sessions WHERE id = :session_id)
              AND s.id <> :session_id
              AND t.question_version_id IS NOT NULL
              AND t.status <> 'PLANNED'
            GROUP BY t.question_version_id
            """
        ),
        {"session_id": session_row["id"]},
    )
    exposure: dict[str, str] = {}
    for row in result.mappings().all():
        last_seen = row["last_seen"]
        exposure[str(row["question_version_id"])] = (
            last_seen.isoformat() if hasattr(last_seen, "isoformat") else str(last_seen or "")
        )
    return exposure


async def _broader_concepts(db: AsyncSession, concept_id: str) -> list[str]:
    """More general skills, nearest first (MySQL -> SQL; LLM -> GenAI -> AI)."""
    result = await db.execute(
        text(
            """
            WITH RECURSIVE up(concept_id, depth) AS (
                SELECT CAST(:concept_id AS text), 0
                UNION
                SELECT r.target_concept_id, up.depth + 1
                FROM taxonomy_relations r JOIN up ON r.source_concept_id = up.concept_id
                WHERE r.relation_type = 'SPECIALIZES' AND up.depth < 3
            )
            SELECT concept_id, MIN(depth) AS depth FROM up
            WHERE depth > 0 GROUP BY concept_id ORDER BY depth, concept_id
            """
        ),
        {"concept_id": concept_id},
    )
    return [str(row["concept_id"]) for row in result.mappings().all()]


async def _role_concepts(db: AsyncSession, concept_id: str, job_role: str | None) -> list[str]:
    """Specialisations that require the skill, the job's own role first."""
    result = await db.execute(
        text(
            "SELECT DISTINCT source_concept_id FROM taxonomy_relations "
            "WHERE relation_type = 'REQUIRES_SKILL' AND target_concept_id = :concept_id"
        ),
        {"concept_id": concept_id},
    )
    roles = sorted(str(row["source_concept_id"]) for row in result.mappings().all())
    if job_role in roles:
        roles.remove(job_role)
        roles.insert(0, job_role)
    return roles


async def _question_skill_concepts(db: AsyncSession, question_version_id: str) -> set[str]:
    result = await db.execute(
        text(
            "SELECT concept_id FROM question_version_taxonomy_concepts "
            "WHERE question_version_id = :id AND purpose IN ('PRIMARY_COMPETENCY', 'TARGET_SKILL')"
        ),
        {"id": question_version_id},
    )
    return {str(row["concept_id"]) for row in result.mappings().all()}


async def _job_taxonomy(db: AsyncSession, job_id: str | None) -> tuple[set[str], str | None]:
    """Concepts the JD requires and its primary specialisation."""
    if not job_id:
        return set(), None
    result = await db.execute(
        text("SELECT structured_data, primary_taxonomy_concept_id FROM job_descriptions WHERE id = :id"),
        {"id": job_id},
    )
    row = result.mappings().one_or_none()
    if not row:
        return set(), None
    data = row.get("structured_data") or {}
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            data = {}
    concepts: set[str] = set()
    for requirement in (data.get("requirements") if isinstance(data, dict) else None) or []:
        if not isinstance(requirement, dict):
            continue
        refs = [requirement.get("concept"), *(requirement.get("atomicConcepts") or [])]
        concepts.update(str(ref["conceptId"]) for ref in refs if isinstance(ref, dict) and ref.get("conceptId"))
    role = row.get("primary_taxonomy_concept_id")
    return concepts, (str(role) if role else None)


async def _fallback_candidates(
    db: AsyncSession,
    *,
    target: dict[str, Any],
    locale: str,
    difficulty: str,
    salt: str,
    job_concepts: set[str],
    job_role: str | None,
    exposure: Mapping[str, str] | None = None,
) -> list[_Candidate]:
    """Questions for a target the bank does not cover, in ladder order.

    1. an approved question on a broader skill (Node.js -> JavaScript);
    2. an approved question for a specialisation that requires the skill, but
       only one whose own skill this JD also asks for -- a backend question
       about Java collections is no substitute for Node.js in a Python JD;
    3. a generated draft for exactly this skill, still waiting for review.
    """
    found: list[_Candidate] = []
    skill_target = {**target, "rationale": {}}
    for concept in await _broader_concepts(db, target["conceptId"]):
        for candidate in await _load_candidates(
            db,
            target={**skill_target, "conceptId": concept},
            locale=locale,
            difficulty=difficulty,
            salt=salt,
            exposure=exposure,
        ):
            found.append(replace(candidate, source="broader_skill"))
    role_target = {**target, "rationale": {"source": "career_classification_fallback"}}
    for role in await _role_concepts(db, target["conceptId"], job_role):
        for candidate in await _load_candidates(
            db, target={**role_target, "conceptId": role}, locale=locale, difficulty=difficulty, salt=salt,
            exposure=exposure,
        ):
            if await _question_skill_concepts(db, candidate.question_version_id) & job_concepts:
                found.append(replace(candidate, source="role"))
    found += await _load_candidates(
        db, target=skill_target, locale=locale, difficulty=difficulty, salt=salt, generated=True,
        exposure=exposure,
    )
    unique: dict[str, _Candidate] = {}
    for candidate in found:
        unique.setdefault(candidate.question_version_id, candidate)
    return list(unique.values())


async def _plan_targets_for_preflight(db: AsyncSession, plan_id: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw_targets = payload.get("targets") if isinstance(payload.get("targets"), list) else None
    if not raw_targets:
        result = await db.execute(
            text(
                "SELECT taxonomy_version, concept_id, label, target_question_count, rationale "
                "FROM session_competency_targets WHERE plan_id = :plan_id"
            ),
            {"plan_id": plan_id},
        )
        raw_targets = [dict(row) for row in result.mappings().all()]
    targets = []
    for item in raw_targets:
        concept_id = item.get("conceptId") or item.get("concept_id")
        if not concept_id:
            continue
        targets.append(
            {
                "taxonomyVersion": item.get("taxonomyVersion") or item.get("taxonomy_version") or "internal-2026.1",
                "conceptId": concept_id,
                "label": item.get("label") or concept_id,
                "needed": max(1, int(item.get("targetQuestionCount") or item.get("target_question_count") or 1)),
                "rationale": item.get("rationale") or {},
            }
        )
    return targets


async def reachable_candidates(
    db: AsyncSession,
    *,
    target: dict[str, Any],
    locale: str,
    difficulty: str,
    job_concepts: set[str],
    job_role: str | None,
    needed: int,
) -> list[_Candidate]:
    """Questions selection can reach for a target, in ladder order.

    Exact approved questions first, then the fallback ladder (broader skill,
    role, generated draft) only when those fall short of ``needed`` -- the
    same reach the selector has. The session preflight and the background
    pre-generation both count with this, so neither generates a question the
    selector would never pick.
    """
    found = await _load_candidates(db, target=target, locale=locale, difficulty=difficulty)
    if len(found) >= needed:
        return found
    seen = {item.question_version_id for item in found}
    extra = await _fallback_candidates(
        db,
        target=target,
        locale=locale,
        difficulty=difficulty,
        salt="",
        job_concepts=job_concepts,
        job_role=job_role,
    )
    return found + [item for item in extra if item.question_version_id not in seen]


async def _generate_missing_questions(
    db: AsyncSession, *, session_row: dict[str, Any], plan_id: str, payload: dict[str, Any]
) -> None:
    """Preflight: file generated drafts for targets nothing else can cover.

    Runs before the plan row is locked: an LLM call can take tens of seconds
    and must not hold the lock. The selection that follows reads the drafts
    through the generated step of the fallback ladder. Skills of published
    JDs are usually covered already by the background pre-generation
    (question_coverage); this is the fallback for the rest.
    """
    from src.modules.interviews.planning.question_generation import ensure_generated_questions

    difficulty = (payload.get("difficulty") or {}).get("level", "unspecified")
    locale = session_row.get("locale") or "en-US"
    job_concepts, job_role = await _job_taxonomy(db, session_row.get("job_id"))
    for target in await _plan_targets_for_preflight(db, plan_id, payload):
        if (target["rationale"] or {}).get("source") == "career_classification_fallback":
            continue
        await ensure_generated_questions(
            db,
            target=target,
            job_concepts=job_concepts,
            job_role=job_role,
            difficulty=difficulty,
            locale=locale,
            desired=target["needed"],
        )


def _snapshot(
    candidate: _Candidate,
    *,
    target: dict[str, Any],
    locale: str,
    selection_rank: int,
    stage: str = "DEEP_DIVE",
) -> dict[str, Any]:
    c_snap = candidate.canonical_snapshot or {}
    return {
        "schemaVersion": "1.0",
        "selectorPolicyVersion": SELECTOR_POLICY_VERSION,
        "questionVersionId": candidate.question_version_id,
        "stableKey": candidate.stable_key,
        "version": candidate.version,
        "questionType": candidate.question_type,
        "stage": stage,
        "difficulty": candidate.difficulty_band,
        "locale": locale,
        "canonicalLocale": candidate.canonical_locale,
        "localizedFrom": candidate.localized_from,
        "questionText": candidate.question_text,
        "objective": candidate.objective,
        "thinkingSeconds": candidate.thinking_seconds,
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
        "questionSource": candidate.source,
        "expectedPoints": candidate.expected_points,
        "rubric": candidate.rubric,
        "canonicalSnapshot": c_snap,
        "starterCode": c_snap.get("starter_code"),
        "testCasesCode": c_snap.get("test_cases_code"),
        "solutionCode": c_snap.get("solution_code"),
        "language": c_snap.get("language", "python"),
    }


def _technical_stage(idx: int, *, total: int, is_coding: bool, is_gap: bool, is_demo: bool) -> str:
    """Stage of the idx-th frozen technical question.

    Coding questions and CV gaps after the first question are CHALLENGE. A demo
    shows every stage, so its last technical question is always CHALLENGE (the
    planner puts the CV gap, if any, in that slot).
    """
    if is_coding or (is_gap and idx >= 1):
        return "CHALLENGE"
    if is_demo and idx >= 1 and idx == total - 1:
        return "CHALLENGE"
    return "DEEP_DIVE"


def _behavioral_snapshot(locale: str) -> dict[str, Any]:
    is_vi = (locale or "vi").lower().startswith("vi")
    prompts_vi = (
        "Kể một lần bạn gặp sự cố kỹ thuật khó hoặc bất đồng trong team: "
        "chuyện gì xảy ra, bạn đã làm gì, kết quả ra sao?"
    )
    prompts_en = (
        "Tell me about a hard technical problem or a disagreement in your team: "
        "what happened, what did you do, and what was the result?"
    )
    return {
        "schemaVersion": "1.0",
        "selectorPolicyVersion": SELECTOR_POLICY_VERSION,
        "questionVersionId": None,
        "stableKey": "behavioral-star-collaboration",
        "version": "1.0.0",
        "questionType": "BEHAVIORAL",
        "stage": "BEHAVIORAL",
        "difficulty": "intermediate",
        "locale": locale,
        "canonicalLocale": locale,
        "questionText": prompts_vi if is_vi else prompts_en,
        "objective": "Đánh giá kỹ năng mềm, giải quyết xung đột và làm việc nhóm theo mô hình STAR.",
        "softAnswerSeconds": 180,
        "hardAnswerSeconds": 240,
        "taxonomyTarget": {
            "taxonomyVersion": "internal-2026.1",
            "conceptId": "soft-skill.star",
            "label": "Kỹ năng mềm & Tình huống (STAR)",
            "mappingPurpose": "BEHAVIORAL",
            "relevance": 1.0,
        },
        "selectionRank": 99,
        "expectedPoints": [],
        "rubric": None,
        "questionSource": "behavioral_star_preset",
    }


def _estimated_cost(candidate: _Candidate) -> int:
    return int(candidate.thinking_seconds or 0) + int(candidate.soft_answer_seconds or 0)


def _matches_archetype(question_type: str, target_archetype: str) -> bool:
    q_type = (question_type or "").strip().lower()
    t_arch = (target_archetype or "").strip().upper()
    if t_arch == "CODING":
        return q_type == "coding"
    elif t_arch == "TEXT":
        return q_type != "coding"
    return q_type == target_archetype.lower()


def _purpose_rank(mapping_purpose: str) -> int:
    return {
        "PRIMARY_COMPETENCY": 0,
        "TARGET_SKILL": 1,
        "TARGET_ROLE": 2,
    }.get(mapping_purpose, 9)


def _subset_tiebreaker(subset: list[_Candidate], session_id: str) -> str:
    sorted_ids = sorted(str(q.question_version_id) for q in subset)
    canonical_seed = f"{session_id}:{':'.join(sorted_ids)}"
    return hashlib.md5(canonical_seed.encode("utf-8")).hexdigest()


def _subset_objective(
    subset: list[_Candidate],
    *,
    time_envelope_seconds: int,
    difficulty: str,
    locale: str,
    session_id: str,
) -> tuple[int, float, float, float, int, str]:
    k = len(subset)
    if k == 0:
        return (99, 99.0, 9.0, 0.0, time_envelope_seconds, "")
    max_locale = max(_locale_rank(q.canonical_locale, locale) for q in subset)
    avg_diff = sum(_difficulty_distance(q.difficulty_band, difficulty) for q in subset) / k
    avg_purpose = sum(_purpose_rank(q.mapping_purpose) for q in subset) / k
    neg_total_relevance = -sum(q.relevance for q in subset)
    duration_eff = time_envelope_seconds - sum(_estimated_cost(q) for q in subset)
    tiebreaker = _subset_tiebreaker(subset, session_id)
    return (
        max_locale,
        round(avg_diff, 6),
        round(avg_purpose, 6),
        round(neg_total_relevance, 6),
        duration_eff,
        tiebreaker,
    )


def _find_feasible_subsets(
    candidates: list[_Candidate],
    *,
    floor_seconds: int,
    time_envelope_seconds: int,
    max_size: int | None = None,
    metrics: dict[str, Any] | None = None,
) -> list[list[_Candidate]]:
    """Enumerate subsets that fit the envelope and reach the time floor.

    The floor is a hard rule (ADR TC-PACK-02: fail closed when unreachable).
    `max_size` bounds the subset size.
    """
    valid_candidates = [c for c in candidates if _estimated_cost(c) <= time_envelope_seconds]
    n = len(valid_candidates)
    if n == 0:
        if metrics is not None:
            metrics["nodes_visited"] = 1
        return []

    costs = [_estimated_cost(c) for c in valid_candidates]
    suffix_costs = [0] * (n + 1)
    for i in range(n - 1, -1, -1):
        suffix_costs[i] = suffix_costs[i + 1] + costs[i]

    if suffix_costs[0] < floor_seconds:
        if metrics is not None:
            metrics["nodes_visited"] = 1
        return []

    feasible_subsets: list[list[_Candidate]] = []
    current_subset: list[_Candidate] = []
    nodes_visited = 0

    def _dfs(idx: int, current_cost: int) -> None:
        nonlocal nodes_visited
        nodes_visited += 1

        if current_subset and current_cost >= floor_seconds:
            feasible_subsets.append(list(current_subset))
        if max_size is not None and len(current_subset) >= max_size:
            return

        for i in range(idx, n):
            c_cost = costs[i]
            if current_cost + c_cost > time_envelope_seconds:
                continue
            if current_cost + c_cost + suffix_costs[i + 1] < floor_seconds:
                continue

            current_subset.append(valid_candidates[i])
            _dfs(i + 1, current_cost + c_cost)
            current_subset.pop()

    _dfs(0, 0)
    if metrics is not None:
        metrics["nodes_visited"] = nodes_visited
    return feasible_subsets


def _generate_target_feasible_subsets(
    candidates: list[_Candidate],
    *,
    target_archetype: str,
    floor_seconds: int,
    time_envelope_seconds: int,
    difficulty: str,
    locale: str,
    session_id: str,
    max_questions: int | None = None,
    metrics: dict[str, Any] | None = None,
) -> list[tuple[tuple[int, float, float, float, int, str], list[_Candidate]]]:
    """Layer A: Generates and ranks all feasible subsets for a single target under R(S).

    `max_questions` caps the subset size at P1's `estimatedQuestionsRange`
    upper bound, and only the best-ranked candidates are enumerated: the
    objective favours larger subsets, so without a cap the queue overshot P1's
    estimate and enumeration grew combinatorially while holding the plan lock.

    Returns list of (subset_objective, ordered_subset_candidates) sorted by R(S) ascending.
    """
    eligible = [c for c in candidates if _matches_archetype(c.question_type, target_archetype)]
    if not eligible:
        if metrics is not None:
            metrics["nodes_visited"] = 1
        return []

    eligible = sorted(
        eligible,
        key=lambda c: _candidate_rank(c, difficulty=difficulty, locale=locale, salt=session_id),
    )[:_SUBSET_CANDIDATE_POOL]

    feasible_subsets = _find_feasible_subsets(
        eligible,
        floor_seconds=floor_seconds,
        time_envelope_seconds=time_envelope_seconds,
        max_size=max_questions,
        metrics=metrics,
    )
    if not feasible_subsets:
        return []

    ranked: list[tuple[tuple[int, float, float, float, int, str], list[_Candidate]]] = []
    for subset in feasible_subsets:
        ordered = sorted(
            subset,
            key=lambda q: _candidate_rank(q, difficulty=difficulty, locale=locale, salt=session_id),
        )
        obj = _subset_objective(
            ordered,
            time_envelope_seconds=time_envelope_seconds,
            difficulty=difficulty,
            locale=locale,
            session_id=session_id,
        )
        ranked.append((obj, ordered))

    ranked.sort(key=lambda item: item[0])
    return ranked


def _pack_target_questions(
    candidates: list[_Candidate],
    *,
    target_archetype: str,
    floor_seconds: int,
    time_envelope_seconds: int,
    difficulty: str,
    locale: str,
    session_id: str,
    metrics: dict[str, Any] | None = None,
) -> list[_Candidate] | None:
    """Convenience helper for single-target packing: returns the highest-ranked feasible subset."""
    subsets = _generate_target_feasible_subsets(
        candidates,
        target_archetype=target_archetype,
        floor_seconds=floor_seconds,
        time_envelope_seconds=time_envelope_seconds,
        difficulty=difficulty,
        locale=locale,
        session_id=session_id,
        metrics=metrics,
    )
    if not subsets:
        return None
    return subsets[0][1]


def _global_assignment_tiebreaker(
    assignment: dict[str, list[_Candidate]],
    targets: list[dict[str, Any]],
    session_id: str,
) -> str:
    """Deterministic tiebreaker for global assignments based on canonical targets and question IDs."""
    entries = []
    for target in targets:
        t_key = f"{target['taxonomyVersion']}:{target['conceptId']}"
        subset = assignment.get(t_key, [])
        ids = sorted(str(q.question_version_id) for q in subset)
        entries.append(f"{target['conceptId']}=" + ",".join(ids))
    seed = f"{session_id}:" + "|".join(entries)
    return hashlib.md5(seed.encode("utf-8")).hexdigest()


def _global_assignment_objective(
    assignment: dict[str, list[_Candidate]],
    target_objectives: dict[str, tuple[int, float, float, float, int, str]],
    targets: list[dict[str, Any]],
    session_id: str,
) -> tuple[Any, ...]:
    """Evaluates Global Objective G(A) = (R(S_1), R(S_2), ..., GlobalTiebreaker(A)) in selectionRank order."""
    objs = []
    for target in targets:
        t_key = f"{target['taxonomyVersion']}:{target['conceptId']}"
        objs.append(target_objectives[t_key])
    tiebreaker = _global_assignment_tiebreaker(assignment, targets, session_id)
    return tuple(objs) + (tiebreaker,)


def _solve_global_question_assignment(
    targets: list[dict[str, Any]],
    target_domains: dict[str, list[tuple[tuple[int, float, float, float, int, str], list[_Candidate]]]],
    session_id: str,
    metrics: dict[str, Any] | None = None,
) -> dict[str, list[_Candidate]] | None:
    """Finds optimal global conflict-free question assignment across all targets (Policy 2).

    Layer B Solver:
    - Target traversal strictly follows canonical selectionRank ASC order.
    - Per-target domains are ordered by subset objective R(S) ASC.
    - Uses Forward Checking for safe pruning: branches with any unassignable remaining target backtrack early.
    - Mathematical invariant: The first complete conflict-free assignment reached by selectionRank-ordered DFS
      is guaranteed to be the exact global lexicographical optimum under G(A).
    - Returns immediately upon finding the first complete assignment without exhaustive search.
    """
    canonical_targets = sorted(
        targets,
        key=lambda t: (
            t["selectionRank"] if t["selectionRank"] is not None else 999999,
            -t.get("importance", 1.0),
            t.get("taxonomyVersion", ""),
            t.get("conceptId", ""),
        ),
    )
    all_target_keys = [f"{t['taxonomyVersion']}:{t['conceptId']}" for t in canonical_targets]
    num_targets = len(all_target_keys)

    # Prune 1: Any target with empty domain fails immediately
    for t_key in all_target_keys:
        if not target_domains.get(t_key):
            if metrics is not None:
                metrics["global_search_nodes"] = 1
                metrics["backtrack_count"] = 0
            return None

    # Pre-extract question_version_id sets for fast O(1) set operations
    # Domains are already sorted by R(S) ascending in Layer A
    prepared_domains: dict[str, list[tuple[tuple[int, float, float, float, int, str], list[_Candidate], set[str]]]] = {}
    for t_key in all_target_keys:
        prepared_domains[t_key] = [
            (obj, subset, {str(q.question_version_id) for q in subset})
            for obj, subset in target_domains[t_key]
        ]

    current_assignment: dict[str, list[_Candidate]] = {}
    used_version_ids: set[str] = set()

    nodes_visited = 0
    backtrack_count = 0

    def _dfs(target_idx: int) -> dict[str, list[_Candidate]] | None:
        nonlocal nodes_visited, backtrack_count

        if target_idx == num_targets:
            # First complete assignment reached is the global lexicographical optimum
            return {k: list(v) for k, v in current_assignment.items()}

        t_key = all_target_keys[target_idx]

        for obj, subset, qids in prepared_domains[t_key]:
            # Conflict-free check with already assigned questions
            if not qids.isdisjoint(used_version_ids):
                continue

            nodes_visited += 1

            current_assignment[t_key] = subset
            used_version_ids.update(qids)

            # Forward checking on all remaining unassigned targets
            has_feasible_completion = True
            for future_idx in range(target_idx + 1, num_targets):
                f_key = all_target_keys[future_idx]
                if not any(item[2].isdisjoint(used_version_ids) for item in prepared_domains[f_key]):
                    has_feasible_completion = False
                    backtrack_count += 1
                    break

            if has_feasible_completion:
                result = _dfs(target_idx + 1)
                if result is not None:
                    return result

            used_version_ids.difference_update(qids)
            del current_assignment[t_key]

        return None

    solution = _dfs(0)

    if metrics is not None:
        metrics["global_search_nodes"] = nodes_visited
        metrics["backtrack_count"] = backtrack_count

    return solution


async def select_and_freeze_questions(
    *,
    db: AsyncSession,
    session_row: dict[str, Any],
) -> dict[str, Any]:
    """Freeze deterministic Question Bank selections and lock the P1 plan."""

    plan_id = session_row.get("plan_id")
    if not plan_id:
        raise ValueError("Interview session has no plan container")

    # Unlocked read: generating drafts for uncovered skills calls the LLM and
    # must finish before the plan row is locked below.
    preflight = await db.execute(
        text("SELECT status, plan_payload FROM interview_session_plans WHERE id = :plan_id AND session_id = :session_id"),
        {"plan_id": plan_id, "session_id": session_row["id"]},
    )
    preflight_plan = preflight.mappings().one_or_none()
    if preflight_plan and preflight_plan["status"] == "READY":
        await _generate_missing_questions(
            db, session_row=session_row, plan_id=plan_id, payload=preflight_plan["plan_payload"] or {}
        )

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
        # Selection is fail-closed on both branches, so no new turn can carry an
        # unreviewed fallback prompt. This still counts them because sessions
        # frozen before that policy may hold such turns, and a report must not
        # silently present them as reviewed question-bank content.
        fallback_count = sum(
            turn["question"].get("questionSource") == "deterministic_fallback_unreviewed"
            for turn in turns
        )
        return {
            "sessionId": session_row["id"],
            "planId": plan_id,
            "status": "LOCKED",
            "selectorPolicyVersion": SELECTOR_POLICY_VERSION,
            "fallbackQuestionCount": fallback_count,
            "turns": turns,
        }
    if plan["status"] != "READY":
        raise RuntimeError("Interview plan must be READY before question selection")

    payload = plan["plan_payload"] or {}
    difficulty = (payload.get("difficulty") or {}).get("level", "unspecified")
    locale = session_row.get("locale") or "en-US"
    policy_version = payload.get("policyVersion")
    is_dynamic = (policy_version == "interview-planner-v2-dynamic")
    # Demo sessions break ties on the stable key instead of the session id, so
    # every demo of a CV-JD pair asks the same questions as the demo script.
    salt = "" if is_demo_duration(session_row.get("duration_minutes")) else str(session_row.get("id") or "")
    job_concepts, job_role = await _job_taxonomy(db, session_row.get("job_id"))
    exposure = await _question_exposure(db, session_row)
    # Targets the ladder could not cover at all, and targets served with fewer
    # questions than planned. Both reach the report as uncovered competencies.
    uncovered_targets: list[dict[str, Any]] = []

    if is_dynamic:
        # -------------------------------------------------------------------
        # Dynamic Branch (Gate 3 Scope A — Two-Phase Subset Packing & Preflight)
        # -------------------------------------------------------------------
        raw_targets = payload.get("targets")
        if not raw_targets or not isinstance(raw_targets, list):
            # Fallback to reading targets from session_competency_targets
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
            raw_targets = [dict(row) for row in targets_result.mappings().all()]

        # P1 deliberately emits a READY plan with no targets when every
        # requirement is not_applicable (or all were omitted). That plan is an
        # onboarding + behavioral interview: freeze only the preset turns
        # instead of failing a plan P1 already accepted.
        raw_targets = raw_targets or []

        targets: list[dict[str, Any]] = []
        for t in raw_targets:
            c_id = t.get("conceptId") or t.get("concept_id")
            t_arch = (t.get("targetArchetype") or t.get("target_archetype") or "TEXT").upper()
            fl_sec = int(t.get("floorSeconds") or t.get("floor_seconds") or (180 if t_arch == "TEXT" else 360))
            env_sec = int(t.get("timeEnvelopeSeconds") or t.get("time_envelope_seconds") or 0)
            targets.append(
                {
                    "selectionRank": int(t.get("selectionRank") if t.get("selectionRank") is not None else 0),
                    "taxonomyVersion": t.get("taxonomyVersion") or t.get("taxonomy_version") or "internal-2026.1",
                    "conceptId": c_id,
                    "label": t.get("label") or c_id,
                    "targetArchetype": t_arch,
                    "timeEnvelopeSeconds": env_sec,
                    "floorSeconds": fl_sec,
                    "estimatedQuestionsRange": t.get("estimatedQuestionsRange") or [1, 2],
                    "importance": float(t.get("importance") if t.get("importance") is not None else 1.0),
                    "targetQuestionCount": int(t.get("targetQuestionCount") if t.get("targetQuestionCount") is not None else 1),
                    "rationale": t.get("rationale") or {},
                }
            )

        targets.sort(
            key=lambda t: (
                t["selectionRank"] if t["selectionRank"] is not None else 999999,
                -t["importance"],
                t["taxonomyVersion"],
                t["conceptId"],
            )
        )

        # Layer A: Load candidate pools and generate feasible subsets per target
        target_domains: dict[str, list[tuple[tuple[int, float, float, float, int, str], list[_Candidate]]]] = {}
        missing_targets: list[dict[str, Any]] = []

        for target in targets:
            t_key = f"{target['taxonomyVersion']}:{target['conceptId']}"
            candidates = await _load_candidates(
                db,
                target=target,
                locale=locale,
                difficulty=difficulty,
                salt=salt,
                exposure=exposure,
            )
            def _subsets(pool: list[_Candidate], target=target) -> list:
                return _generate_target_feasible_subsets(
                    pool,
                    target_archetype=target["targetArchetype"],
                    floor_seconds=target["floorSeconds"],
                    time_envelope_seconds=target["timeEnvelopeSeconds"],
                    difficulty=difficulty,
                    locale=locale,
                    session_id=salt,
                    max_questions=max(1, int(target["estimatedQuestionsRange"][-1])),
                )

            feasible_subsets = _subsets(candidates)
            if not feasible_subsets:
                seen = {item.question_version_id for item in candidates}
                extra = await _fallback_candidates(
                    db,
                    target=target,
                    locale=locale,
                    difficulty=difficulty,
                    salt=salt,
                    job_concepts=job_concepts,
                    job_role=job_role,
                    exposure=exposure,
                )
                feasible_subsets = _subsets(candidates + [c for c in extra if c.question_version_id not in seen])
            if not feasible_subsets:
                missing_targets.append(
                    {
                        "conceptId": target["conceptId"],
                        "label": target["label"],
                        "targetArchetype": target["targetArchetype"],
                        "floorSeconds": target["floorSeconds"],
                        "timeEnvelopeSeconds": target["timeEnvelopeSeconds"],
                        "reason": "no_feasible_qualifying_subset",
                    }
                )
            else:
                target_domains[t_key] = feasible_subsets

        if missing_targets and len(missing_targets) == len(targets):
            # Nothing technical can be asked: 0 turns written, plan not locked.
            raise QuestionUnavailableError(
                "question_bank_insufficient: no interview target could be covered by the question bank",
                error_code="question_bank_insufficient",
                details={"missingTargets": missing_targets},
            )
        # Drop what even the fallback ladder could not cover; the report lists it.
        uncovered_targets.extend(missing_targets)
        targets = [t for t in targets if f"{t['taxonomyVersion']}:{t['conceptId']}" in target_domains]

        # Layer B: Global Allocation Search (Policy 2 — Global Constraint Satisfaction / Backtracking)
        global_assignment = _solve_global_question_assignment(
            targets=targets,
            target_domains=target_domains,
            session_id=str(session_row.get("id") or ""),
        )

        if global_assignment is None:
            raise QuestionUnavailableError(
                "question_bank_insufficient: Question bank cannot satisfy conflict-free global target allocation under fail-closed policy",
                error_code="question_bank_insufficient",
                details={"reason": "no_conflict_free_global_assignment"},
            )

        # Freeze questions in canonical pedagogical order (selectionRank ASC)
        frozen: list[tuple[_Candidate | None, dict[str, Any], int, dict[str, Any] | None]] = []
        for target in targets:
            t_key = f"{target['taxonomyVersion']}:{target['conceptId']}"
            assigned_subset = global_assignment[t_key]
            for candidate in assigned_subset:
                frozen.append((candidate, target, len(frozen), None))

    else:
        # -------------------------------------------------------------------
        # Legacy Branch (policyVersion = interview-planner-v1 or default)
        # Preserves existing behavior, coding replacement and trace logging
        # -------------------------------------------------------------------
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

        used_versions: set[str] = set()
        frozen: list[tuple[_Candidate | None, dict[str, Any], int, dict[str, Any] | None]] = []

        for target in targets:
            candidates = await _load_candidates(
                db,
                target=target,
                locale=locale,
                difficulty=difficulty,
                salt=salt,
                exposure=exposure,
            )
            available = [item for item in candidates if item.question_version_id not in used_versions]
            # Honour the planner's per-target count exactly. P1 already guarantees
            # a floor of one question per competency and caps the sum at
            # questionBudget, so the old `max(count, 2)` floor silently doubled
            # the technical queue: four targets at one question each became eight
            # frozen turns, which cannot fit in a 25-minute session and broke the
            # documented `targetQuestionCount <= questionBudget` invariant.
            #
            # The coding-question swap is gone for the same reason. Replacing a
            # ranked pick with an arbitrary coding question changed the agenda and
            # the answer-time envelope behind the planner's back; question type is
            # a planner/Question-Bank concern, not something P2 may re-decide.
            needed = max(1, int(target["targetQuestionCount"]))
            selected = available[:needed]

            if len(selected) < needed:
                # Fallback ladder: broader skill -> role question -> generated draft.
                taken = used_versions | {item.question_version_id for item in selected}
                extra = await _fallback_candidates(
                    db,
                    target=target,
                    locale=locale,
                    difficulty=difficulty,
                    salt=salt,
                    job_concepts=job_concepts,
                    job_role=job_role,
                    exposure=exposure,
                )
                selected += [c for c in extra if c.question_version_id not in taken][: needed - len(selected)]

            if len(selected) < needed:
                # Covered partially or not at all: interview what we can and
                # report the rest instead of refusing the whole session.
                uncovered_targets.append(
                    {
                        "taxonomyVersion": target["taxonomyVersion"],
                        "conceptId": target["conceptId"],
                        "label": target["label"],
                        "needed": needed,
                        "available": len(selected),
                        "reason": "no_question_after_fallback",
                    }
                )

            for candidate in selected:
                frozen.append((candidate, target, len(frozen), None))
                used_versions.add(candidate.question_version_id)

        if not frozen:
            # No target could be served at all: 0 turns written, plan not locked.
            raise QuestionUnavailableError(
                "Question bank cannot satisfy interview plan requirements",
                error_code="question_bank_insufficient",
                details={"missingTargets": uncovered_targets},
            )

    # Do not partially write turns before every target is satisfiable.
    await db.execute(
        text("DELETE FROM interview_turns WHERE session_id = :session_id"),
        {"session_id": session_row["id"]},
    )

    is_vi = (locale or "vi").lower().startswith("vi")
    job_title = "ứng viên"
    if session_row.get("job_id"):
        title_res = await db.scalar(
            text("SELECT title FROM job_descriptions WHERE id = :job_id"),
            {"job_id": session_row["job_id"]},
        )
        if title_res:
            job_title = str(title_res)

    project_name = None
    key_technologies: list[str] = []
    selected_project = None
    all_project_evidences = []

    # Retrieve evaluation targets and match statuses from session plan if present
    eval_targets = []
    req_match_statuses = {}
    if session_row.get("id"):
        plan_row = await db.execute(
            text("SELECT plan_payload FROM interview_session_plans WHERE session_id = :sid"),
            {"sid": session_row["id"]},
        )
        p_row = plan_row.mappings().first()
        if p_row and p_row.get("plan_payload"):
            payload = p_row["plan_payload"]
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except Exception:
                    payload = {}
            if isinstance(payload, dict):
                eval_targets = payload.get("evaluationTargets") or []
                for et in eval_targets:
                    if isinstance(et, dict) and et.get("requirementId"):
                        req_match_statuses[et["requirementId"]] = et.get("status", "unknown")

    if session_row.get("resume_id"):
        cv_res = await db.scalar(
            text("SELECT parsed_data FROM user_cvs WHERE id = :cv_id"),
            {"cv_id": session_row["resume_id"]},
        )
        if isinstance(cv_res, str):
            try:
                cv_res = json.loads(cv_res)
            except Exception:
                cv_res = {}
        if cv_res and isinstance(cv_res, dict):
            # 1. Trích xuất và xếp hạng dự án theo JD relevance
            projects = cv_res.get("projects") or []
            cv_skills = cv_res.get("skills") or []
            all_project_evidences = extract_project_evidences(
                projects_data=projects,
                job_requirements=eval_targets,
                cv_skills=cv_skills,
                requirement_match_statuses=req_match_statuses,
            )
            selected_project = select_best_project(all_project_evidences)
            if selected_project:
                project_name = selected_project.name

            # 2. Nếu không có mục projects riêng, trích xuất từ kinh nghiệm làm việc (employment)
            if not project_name:
                employment = cv_res.get("employment") or []
                if employment and isinstance(employment, list):
                    for emp in employment:
                        if isinstance(emp, dict):
                            role = emp.get("job_title")
                            org = emp.get("organization")
                            if role and org:
                                project_name = f"{role} tại {org}"
                                break
                            elif role:
                                project_name = role
                                break

            # 3. Trích xuất kỹ năng / công nghệ cốt lõi
            skills = cv_res.get("skills") or []
            if skills and isinstance(skills, list):
                for s in skills:
                    if isinstance(s, dict):
                        label = (
                            s.get("raw_label")
                            or (s.get("concept") or {}).get("label")
                            or (s.get("concept") or {}).get("concept_id")
                        )
                        if label and str(label).strip() not in key_technologies:
                            key_technologies.append(str(label).strip())
                    elif isinstance(s, str) and s.strip() not in key_technologies:
                        key_technologies.append(s.strip())

    warmup_text = (
        f"Chào bạn, mình là người phỏng vấn vị trí {job_title}. "
        "Bạn giới thiệu ngắn về bản thân và kinh nghiệm gần nhất nhé?"
        if is_vi
        else f"Hi, I'm your interviewer for the {job_title} role. "
             "Could you briefly introduce yourself and your most recent experience?"
    )

    if selected_project:
        validate_text = build_project_validation_question(
            project=selected_project,
            job_title=job_title,
            locale=locale,
        )
        trace_event(
            "interviewer",
            "project_evidence_selected",
            session_id=session_row.get("id"),
            selected_project=selected_project.name,
            project_id=selected_project.project_id,
            jd_relevance_score=selected_project.jd_relevance_score,
            richness_score=selected_project.evidence_richness_score,
            role_status=selected_project.role_status,
            technologies_status=selected_project.technologies_status,
            outcomes_status=selected_project.outcomes_status,
            relevant_requirements=selected_project.relevant_requirements,
            candidate_projects_count=len(all_project_evidences),
            selection_reason=selected_project.selection_reason,
        )
    elif project_name:
        validate_text = (
            f"Trong '{project_name}', bạn trực tiếp làm phần nào, và bài toán khó nhất bạn giải quyết là gì?"
            if is_vi
            else f"In '{project_name}', which parts did you do yourself, and what was the hardest problem you solved?"
        )
    elif key_technologies:
        tech_str = ", ".join(key_technologies[:3])
        validate_text = (
            f"CV của bạn có {tech_str}. Kể một bài toán gần đây bạn giải quyết bằng các công nghệ này?"
            if is_vi
            else f"Your CV lists {tech_str}. What is a recent problem you solved with them?"
        )
    else:
        validate_text = (
            "Dự án kỹ thuật nổi bật nhất của bạn là gì: bạn làm phần nào, và bài toán khó nhất là gì?"
            if is_vi
            else "What is your most notable technical project: which part was yours, and what was the hardest problem?"
        )

    warmup_snapshot = {
        "schemaVersion": "1.0",
        "selectorPolicyVersion": SELECTOR_POLICY_VERSION,
        "questionVersionId": None,
        "stableKey": "warmup-intro",
        "version": "1.0.0",
        "questionType": "WARM_UP",
        "stage": "WARM_UP",
        "difficulty": "foundational",
        "locale": locale,
        "canonicalLocale": locale,
        "questionText": warmup_text,
        "objective": "Chào hỏi, tạo không khí thoải mái và giới thiệu bản thân.",
        "softAnswerSeconds": 120,
        "hardAnswerSeconds": 180,
        "taxonomyTarget": {
            "taxonomyVersion": "internal-2026.1",
            "conceptId": "warmup",
            "label": "Giới thiệu & Khởi động",
            "mappingPurpose": "WARM_UP",
            "relevance": 1.0,
        },
        "selectionRank": 0,
        "expectedPoints": [],
        "rubric": None,
    }

    validate_snapshot = {
        "schemaVersion": "1.0",
        "selectorPolicyVersion": SELECTOR_POLICY_VERSION,
        "questionVersionId": None,
        "stableKey": "cv-validation",
        "version": "1.0.0",
        "questionType": "VALIDATE_CV",
        "stage": "VALIDATE",
        "difficulty": "intermediate",
        "locale": locale,
        "canonicalLocale": locale,
        "questionText": validate_text,
        "objective": "Xác thực kinh nghiệm thực tế và dự án kỹ thuật tiêu biểu trong CV.",
        "softAnswerSeconds": 180,
        "hardAnswerSeconds": 240,
        "taxonomyTarget": {
            "taxonomyVersion": "internal-2026.1",
            "conceptId": "cv-validation",
            "label": "Xác thực CV & Dự án",
            "mappingPurpose": "VALIDATE",
            "relevance": 1.0,
        },
        "selectionRank": 1,
        "expectedPoints": [],
        "rubric": None,
        "projectEvidence": selected_project.to_dict() if selected_project else None,
        "projectName": selected_project.name if selected_project else project_name,
        "projectId": selected_project.project_id if selected_project else None,
    }

    # Insert Turn 0 (WARM_UP)
    await db.execute(
        text(
            """
            INSERT INTO interview_turns
                (id, session_id, turn_index, status, question_version_id,
                 rubric_version_id, question_snapshot)
            VALUES
                (:id, :session_id, 0, 'PLANNED',
                 NULL, NULL, CAST(:snapshot AS jsonb))
            """
        ),
        {
            "id": str(uuid4()),
            "session_id": session_row["id"],
            "snapshot": json.dumps(warmup_snapshot),
        },
    )

    # Insert Turn 1 (VALIDATE)
    await db.execute(
        text(
            """
            INSERT INTO interview_turns
                (id, session_id, turn_index, status, question_version_id,
                 rubric_version_id, question_snapshot)
            VALUES
                (:id, :session_id, 1, 'PLANNED',
                 NULL, NULL, CAST(:snapshot AS jsonb))
            """
        ),
        {
            "id": str(uuid4()),
            "session_id": session_row["id"],
            "snapshot": json.dumps(validate_snapshot),
        },
    )

    # Deep-dive and challenge technical questions from Question Bank start at turn_index = 2
    is_demo = is_demo_duration(session_row.get("duration_minutes"))
    for idx, (candidate, target, _orig_rank, fallback) in enumerate(frozen):
        turn_index = idx + 2
        target_rationale = target.get("rationale") or {}
        match_statuses = target_rationale.get("matchStatuses") or []
        stage = _technical_stage(
            idx,
            total=len(frozen),
            is_coding=(candidate.question_type == "coding") if candidate else False,
            is_gap=any(s in ("not_met", "unknown") for s in match_statuses),
            is_demo=is_demo,
        )
        if candidate is None:
            snapshot = dict(fallback or {})
            snapshot["selectionRank"] = turn_index
            snapshot["stage"] = stage
        else:
            snapshot = _snapshot(
                candidate,
                target=target,
                locale=locale,
                selection_rank=turn_index,
                stage=stage,
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
                "question_version_id": candidate.question_version_id if candidate else None,
                "rubric_version_id": candidate.rubric_version_id if candidate else None,
                "snapshot": json.dumps(snapshot),
            },
        )

    # Thêm câu hỏi BEHAVIORAL theo chuẩn STAR vào cuối Question Queue
    behavioral_snap = _behavioral_snapshot(locale=locale)
    behavioral_turn_index = len(frozen) + 2
    behavioral_snap["selectionRank"] = behavioral_turn_index
    await db.execute(
        text(
            """
            INSERT INTO interview_turns
                (id, session_id, turn_index, status, question_version_id,
                 rubric_version_id, question_snapshot)
            VALUES
                (:id, :session_id, :turn_index, 'PLANNED',
                 NULL, NULL, CAST(:snapshot AS jsonb))
            """
        ),
        {
            "id": str(uuid4()),
            "session_id": session_row["id"],
            "turn_index": behavioral_turn_index,
            "snapshot": json.dumps(behavioral_snap),
        },
    )

    selection_contract = [
        {"turnIndex": 0, "stage": "WARM_UP"},
        {"turnIndex": 1, "stage": "VALIDATE"},
    ] + [
        {
            "turnIndex": idx + 2,
            "questionVersionId": candidate.question_version_id if candidate else None,
            "rubricVersionId": candidate.rubric_version_id if candidate else None,
            "questionSource": candidate.source if candidate else "deterministic_fallback_unreviewed",
            "fallbackKey": fallback["stableKey"] if fallback else None,
            "target": f"{target['taxonomyVersion']}:{target['conceptId']}",
        }
        for idx, (candidate, target, _, fallback) in enumerate(frozen)
    ] + [
        {
            "turnIndex": behavioral_turn_index,
            "stage": "BEHAVIORAL",
            "questionSource": "behavioral_star_preset",
        }
    ]
    source_counts: dict[str, int] = {}
    for candidate, _, _, _ in frozen:
        if candidate is not None:
            source_counts[candidate.source] = source_counts.get(candidate.source, 0) + 1
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
                        "turnCount": len(selection_contract),
                        "technicalQuestionCount": len(frozen),
                        "fallbackQuestionCount": sum(candidate is None for candidate, _, _, _ in frozen),
                        "questionSources": source_counts,
                        "generatedQuestionCount": source_counts.get("generated_unreviewed", 0),
                        "uncoveredTargets": uncovered_targets,
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
        "turnCount": len(selection_contract),
        "technicalQuestionCount": len(frozen),
        "fallbackQuestionCount": sum(candidate is None for candidate, _, _, _ in frozen),
        "generatedQuestionCount": source_counts.get("generated_unreviewed", 0),
        "uncoveredTargets": uncovered_targets,
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
