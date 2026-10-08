"""Deterministic P2 question selector for the structured interview runtime.

P2 consumes a READY P1 plan and freezes qualifying Question Bank versions into
interview_turns. If any target lacks enough eligible questions, selection fails
closed with ``question_bank_insufficient`` before writing any turns. Existing
frozen fallback turns remain readable, but this selector does not create new
fallback turns for a queue.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.trace_logging import trace_event
from src.modules.interviews.planning.project_evidence import (
    build_project_validation_question,
    extract_project_evidences,
    select_best_project,
)

SELECTOR_POLICY_VERSION = "interview-question-selector-v2"
_ELIGIBLE_STATUSES = {"APPROVED", "CALIBRATED"}
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
    return (
        _locale_rank(candidate.canonical_locale, locale),
        _difficulty_distance(candidate.difficulty_band, difficulty),
        purpose_rank,
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
) -> list[_Candidate]:
    purposes = _allowed_purposes(target)
    result = await db.execute(
        text(
            """
            SELECT q.stable_key, qv.id AS question_version_id, qv.version,
                   qv.status, qv.question_type, qv.difficulty_band,
                   qv.canonical_locale, qv.canonical_text, qv.objective,
                   qv.thinking_seconds, qv.soft_answer_seconds, qv.hard_answer_seconds,
                   qv.canonical_snapshot,
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
              AND map.concept_id = :concept_id
              AND (
                  map.taxonomy_version = :taxonomy_version
                  OR (
                      :taxonomy_version IN ('internal-2026.1', 'internal-career-2026.1')
                      AND map.taxonomy_version IN ('internal-2026.1', 'internal-career-2026.1')
                  )
              )
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
                thinking_seconds=int(row.get("thinking_seconds") or 0),
                canonical_snapshot=dict(row.get("canonical_snapshot") or {}),
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
        key=lambda item: _candidate_rank(item, difficulty=difficulty, locale=locale, salt=salt),
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
        "expectedPoints": candidate.expected_points,
        "rubric": candidate.rubric,
        "canonicalSnapshot": c_snap,
        "starterCode": c_snap.get("starter_code"),
        "testCasesCode": c_snap.get("test_cases_code"),
        "solutionCode": c_snap.get("solution_code"),
        "language": c_snap.get("language", "python"),
    }


def _fallback_snapshot(
    target: dict[str, Any],
    *,
    locale: str,
    target_question_index: int,
) -> dict[str, Any]:
    """Build a stable, competency-specific prompt when the bank has no match.

    This is a continuity path, not a substitute for reviewed question-bank
    content. The explicit source marker lets the evaluator and UI distinguish
    these prompts from calibrated questions.
    """
    label = str(target.get("label") or target.get("conceptId") or "the target competency").strip()
    is_vi = (locale or "vi").lower().startswith("vi")
    prompts_vi = (
        f"Hãy kể về một tình huống thực tế bạn đã vận dụng {label}. Bối cảnh là gì, "
        "bạn trực tiếp chịu trách nhiệm phần nào và kết quả ra sao?",
        f"Khi xử lý một vấn đề liên quan đến {label}, bạn thường phân tích và chọn hướng giải quyết như thế nào? "
        "Hãy nêu một ví dụ cụ thể và giải thích các bước của bạn.",
        f"Hãy mô tả một quyết định khó liên quan đến {label}. Bạn đã cân nhắc những phương án và đánh đổi nào, "
        "và nhìn lại bạn sẽ làm gì khác?",
    )
    prompts_en = (
        f"Tell me about a real situation where you applied {label}. What was the context, "
        "what were you personally responsible for, and what was the outcome?",
        f"How do you analyze and solve a problem involving {label}? Give a specific example and walk me through your steps.",
        f"Describe a difficult decision involving {label}. What options and trade-offs did you consider, "
        "and what would you do differently in retrospect?",
    )
    prompt_index = min(max(target_question_index, 0), 2)
    return {
        "schemaVersion": "1.0",
        "selectorPolicyVersion": SELECTOR_POLICY_VERSION,
        "questionVersionId": None,
        "stableKey": f"fallback-{target['taxonomyVersion']}-{target['conceptId']}-{prompt_index + 1}",
        "version": "1.0.0",
        "questionType": "COMPETENCY_FALLBACK",
        "stage": "DEEP_DIVE",
        "difficulty": "intermediate",
        "locale": locale,
        "canonicalLocale": locale,
        "questionText": (prompts_vi if is_vi else prompts_en)[prompt_index],
        "objective": f"Elicit concrete evidence of the candidate's {label} competency.",
        "softAnswerSeconds": 180,
        "hardAnswerSeconds": 300,
        "taxonomyTarget": {
            "taxonomyVersion": target["taxonomyVersion"],
            "conceptId": target["conceptId"],
            "label": label,
            "mappingPurpose": "DETERMINISTIC_FALLBACK",
            "relevance": 1.0,
        },
        "selectionRank": None,
        "expectedPoints": [],
        "rubric": None,
        "questionSource": "deterministic_fallback_unreviewed",
    }


def _behavioral_snapshot(locale: str) -> dict[str, Any]:
    is_vi = (locale or "vi").lower().startswith("vi")
    prompts_vi = (
        "Hãy kể về một tình huống thực tế khi bạn phải đối mặt với một vấn đề kỹ thuật khó "
        "hoặc bất đồng ý kiến trong đội ngũ. Bạn đã phân tích, giải quyết tình huống đó như thế nào (theo mô hình STAR) và kết quả ra sao?"
    )
    prompts_en = (
        "Describe a challenging situation at work where you faced a tough technical hurdle or a disagreement in your team. "
        "How did you address the challenge using the STAR approach, and what was the outcome?"
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
    metrics: dict[str, Any] | None = None,
) -> list[list[_Candidate]]:
    """Legacy helper: enumerates all feasible subsets satisfying floor and envelope."""
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
    metrics: dict[str, Any] | None = None,
) -> list[tuple[tuple[int, float, float, float, int, str], list[_Candidate]]]:
    """Layer A: Generates and ranks all feasible subsets for a single target under R(S).

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
    )

    feasible_subsets = _find_feasible_subsets(
        eligible,
        floor_seconds=floor_seconds,
        time_envelope_seconds=time_envelope_seconds,
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

        if not raw_targets:
            raise QuestionUnavailableError(
                "question_unavailable: interview plan has no competency targets",
                error_code="question_bank_insufficient",
            )

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
                salt=str(session_row.get("id") or ""),
            )
            feasible_subsets = _generate_target_feasible_subsets(
                candidates,
                target_archetype=target["targetArchetype"],
                floor_seconds=target["floorSeconds"],
                time_envelope_seconds=target["timeEnvelopeSeconds"],
                difficulty=difficulty,
                locale=locale,
                session_id=str(session_row.get("id") or ""),
            )
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

        if missing_targets:
            # Atomic Fail-Closed: 0 turns written, plan not locked
            raise QuestionUnavailableError(
                "question_bank_insufficient: Question bank cannot satisfy interview plan requirements under fail-closed policy",
                error_code="question_bank_insufficient",
                details={"missingTargets": missing_targets},
            )

        # Layer B: Global Allocation Search (Policy 2 — Global Constraint Satisfaction / Backtracking)
        global_assignment = _solve_global_question_assignment(
            targets=targets,
            target_domains=target_domains,
            session_id=str(session_row.get("id") or ""),
        )

        if not global_assignment:
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
                salt=str(session_row.get("id") or ""),
            )
            available = [item for item in candidates if item.question_version_id not in used_versions]
            needed = max(target["targetQuestionCount"], 2)
            selected = available[:needed]
            # Guarantee technical diversity: if eligible coding questions exist in available,
            # ensure at least one coding question is represented in the selected batch.
            if needed >= 2 and not any(c.question_type == "coding" for c in selected):
                coding_cand = next((c for c in available if c.question_type == "coding"), None)
                if coding_cand:
                    selected = selected[:needed - 1] + [coding_cand]

            if len(selected) < needed:
                # Preserve the v1 target-count rule (including its existing floor of 2),
                # but never turn a new session into an unreviewed fallback interview.
                # No turn/plan writes occur until every target has passed this preflight.
                raise QuestionUnavailableError(
                    "Question bank cannot satisfy interview plan requirements",
                    error_code="question_bank_insufficient",
                    details={
                        "missingTargets": [
                            {
                                "taxonomyVersion": target["taxonomyVersion"],
                                "conceptId": target["conceptId"],
                                "needed": needed,
                                "available": len(selected),
                            }
                        ]
                    },
                )

            for candidate in selected:
                frozen.append((candidate, target, len(frozen), None))
                used_versions.add(candidate.question_version_id)

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
        f"Chào bạn, chào mừng bạn đến với buổi phỏng vấn vị trí {job_title} tại INTERVIA. "
        f"Để bắt đầu và giúp bạn thoải mái hơn, bạn hãy giới thiệu đôi nét về bản thân và kinh nghiệm làm việc gần đây của mình nhé?"
        if is_vi
        else f"Hello and welcome to the interview for the {job_title} position at INTERVIA. "
             f"To help you get comfortable, please give a brief introduction of yourself and your recent experience."
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
            f"Cảm ơn phần giới thiệu của bạn. Trong CV mình rất ấn tượng với dự án '{project_name}'. "
            f"Bạn có thể chia sẻ cụ thể hơn về vai trò của bạn trong dự án này, "
            f"và bài toán kỹ thuật phức tạp nhất mà bạn đã trực tiếp giải quyết là gì không?"
            if is_vi
            else f"Thank you for your introduction. Looking at your CV, I was very interested in the '{project_name}' project. "
                 f"Could you share more specifically about your role in this project, "
                 f"and what was the most complex technical problem you directly solved?"
        )
    elif key_technologies:
        tech_str = ", ".join(key_technologies[:3])
        validate_text = (
            f"Cảm ơn bạn. Nhìn vào CV, mình thấy bạn có thế mạnh về {tech_str}. "
            f"Bạn có thể chia sẻ về một bài toán kỹ thuật thực tế gần đây nhất mà bạn áp dụng các công nghệ này không?"
            if is_vi
            else f"Thank you. Looking at your CV, I see you have strong background in {tech_str}. "
                 f"Could you share a recent practical technical problem where you applied these technologies?"
        )
    else:
        validate_text = (
            "Cảm ơn phần giới thiệu của bạn. Nhìn vào hồ sơ CV của bạn, bạn có thể chia sẻ sâu hơn về một dự án "
            "kỹ thuật nổi bật nhất mà bạn từng tham gia: vai trò cụ thể của bạn và bài toán khó nhất bạn đã trực tiếp giải quyết là gì không?"
            if is_vi
            else "Thank you for your introduction. Looking at your CV, could you share more details about your most "
                 "prominent technical project: your specific role and the hardest problem you directly solved?"
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
    for idx, (candidate, target, _orig_rank, fallback) in enumerate(frozen):
        turn_index = idx + 2
        target_rationale = target.get("rationale") or {}
        match_statuses = target_rationale.get("matchStatuses") or []
        # Nếu là câu hỏi coding hoặc gap kỹ năng -> gắn nhãn CHALLENGE
        is_coding = (candidate.question_type == "coding") if candidate else False
        is_gap = any(s in ("not_met", "unknown") for s in match_statuses)
        stage = "CHALLENGE" if (is_coding or (is_gap and idx >= 1)) else "DEEP_DIVE"
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
            "questionSource": "question_bank" if candidate else "deterministic_fallback_unreviewed",
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
