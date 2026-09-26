"""Deterministic P2 question selector for the structured interview runtime.

P2 consumes a READY P1 plan and freezes Question Bank versions into
interview_turns. When the curated bank cannot cover a target, it freezes a
deterministic competency prompt so the interview can still run; snapshots mark
these prompts as uncalibrated fallbacks for downstream evaluation.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

SELECTOR_POLICY_VERSION = "interview-question-selector-v2"
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
                   qv.soft_answer_seconds, qv.hard_answer_seconds,
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
        needed = target["targetQuestionCount"]
        selected = available[:needed]
        for candidate in selected:
            frozen.append((candidate, target, len(frozen), None))
            used_versions.add(candidate.question_version_id)
        for fallback_index in range(needed - len(selected)):
            fallback = _fallback_snapshot(
                target,
                locale=locale,
                target_question_index=fallback_index,
            )
            frozen.append((None, target, len(frozen), fallback))

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
            # 1. Trích xuất dự án tiêu biểu từ mục projects
            projects = cv_res.get("projects") or []
            if projects and isinstance(projects, list):
                for p in projects:
                    if isinstance(p, dict) and p.get("name"):
                        project_name = str(p["name"]).strip()
                        break
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

    if project_name:
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
        # Nếu competency là gap kỹ năng (chưa có trong CV hoặc unknown) -> gắn nhãn CHALLENGE
        # Nếu competency là thế mạnh đã có trong CV (met) -> gắn nhãn DEEP_DIVE
        is_gap = any(s in ("not_met", "unknown") for s in match_statuses)
        stage = "CHALLENGE" if is_gap and idx >= 1 else "DEEP_DIVE"
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
                        "turnCount": len(frozen) + 2,
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
