"""Post-session Question Bank suggestions from recorded coverage gaps."""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import get_settings
from src.modules.interviews.core.session_policy.demo_mode import is_demo_duration
from src.modules.interviews.question_bank.generation import ensure_generated_questions
from src.modules.interviews.agents.planner.selection import _job_taxonomy

logger = logging.getLogger(__name__)
SUGGEST_TASK_NAME = "question_bank.suggest_session_gaps"


async def suggest_questions_for_closed_session(db: AsyncSession, *, session_id: str) -> dict[str, Any]:
    """Generate review-only drafts for uncovered targets from a closed, frozen session."""
    if not get_settings().question_generation_enabled:
        return {"sessionId": session_id, "status": "SKIPPED", "reason": "generation_disabled"}
    result = await db.execute(
        text(
            "SELECT s.status AS session_status, s.job_id, s.locale, s.duration_minutes, "
            "p.status AS plan_status, p.plan_payload, "
            "EXISTS (SELECT 1 FROM interview_evaluations e WHERE e.session_id = s.id) AS has_evaluation "
            "FROM interview_sessions s JOIN interview_session_plans p ON p.session_id = s.id "
            "WHERE s.id = :sid"
        ),
        {"sid": session_id},
    )
    row = result.mappings().one_or_none()
    if row is None or row["session_status"] != "CLOSED" or row["plan_status"] != "LOCKED":
        return {"sessionId": session_id, "status": "SKIPPED", "reason": "session_not_closed_and_frozen"}
    if not row["has_evaluation"]:
        return {"sessionId": session_id, "status": "SKIPPED", "reason": "evaluation_not_available"}
    if is_demo_duration(row["duration_minutes"]):
        return {"sessionId": session_id, "status": "SKIPPED", "reason": "demo_session"}

    payload = row["plan_payload"] or {}
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            payload = {}
    question_selection = payload.get("questionSelection") if isinstance(payload, dict) else {}
    gaps = (question_selection or {}).get("uncoveredTargets") or []
    if not gaps:
        return {"sessionId": session_id, "status": "COMPLETE", "targets": 0, "generated": 0}

    job_concepts, job_role = await _job_taxonomy(db, row["job_id"])
    difficulty = (payload.get("difficulty") or {}).get("level", "unspecified")
    generated = 0
    processed = 0
    for gap in gaps:
        concept_id = str(gap.get("conceptId") or "").strip()
        if not concept_id:
            continue
        needed = max(1, int(gap.get("needed") or 1))
        target = {
            "taxonomyVersion": str(gap.get("taxonomyVersion") or "internal-2026.1"),
            "conceptId": concept_id,
            "label": str(gap.get("label") or concept_id),
            "rationale": {"source": "closed_session_coverage_gap"},
        }
        processed += 1
        try:
            generated += await ensure_generated_questions(
                db,
                target=target,
                job_concepts=job_concepts,
                job_role=job_role,
                difficulty=difficulty,
                locale=row["locale"] or "vi-VN",
                desired=needed,
            )
        except Exception:  # One target failure must not discard other suggestions.
            logger.exception("Question Bank suggestion failed for %s/%s", session_id, concept_id)
    return {"sessionId": session_id, "status": "COMPLETE", "targets": processed, "generated": generated}


def enqueue_session_question_suggestions(session_id: str) -> bool:
    """Queue post-score gap suggestions; broker errors never invalidate scoring."""
    if not get_settings().question_generation_enabled:
        return False
    try:
        from src.workers.celery_app import celery_app

        celery_app.send_task(SUGGEST_TASK_NAME, args=[{"session_id": session_id}])
        return True
    except Exception:
        logger.exception("Could not enqueue post-session question suggestions for %s", session_id)
        return False
