"""Question Bank coverage for published jobs: background pre-generation and a coverage report.

Generating a question while a candidate waits for their session is slow and
fails with the LLM provider. When a job is published (ingested, finalized,
activated), a background task makes sure every must-have skill of the job
already has MIN_QUESTIONS_PER_SKILL questions the selector can reach -- enough
to rotate between sessions -- and generates gated drafts for the rest.

Only must-have skills are prepared, to bound cost: a preferred skill that a
25- or 45-minute plan still selects falls back to session-time generation.

The report answers "how well does the bank cover real sessions and jobs?"
from data the selector already persists (`questionSelection` in the plan).
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import get_settings
from src.modules.interviews.core.demo_mode import DEMO_DURATION_MINUTES
from src.modules.interviews.planning.plan_structure import derive_difficulty
from src.modules.interviews.planning.planner import _requirement_concepts
from src.modules.interviews.planning.question_generation import (
    MAX_PREGENERATED_PER_TARGET,
    ensure_generated_questions,
)
from src.modules.interviews.planning.question_selector import _job_taxonomy, reachable_candidates
from src.modules.job_descriptions.schemas import CanonicalJobDescription
from src.modules.matching.facade import canonical_job_from_description
from src.modules.matching.schemas import CanonicalJob

logger = logging.getLogger(__name__)

MIN_QUESTIONS_PER_SKILL = MAX_PREGENERATED_PER_TARGET
# Sessions are created in the product language; generated drafts follow it.
PREGENERATION_LOCALE = "vi-VN"
PREPARE_TASK_NAME = "question_bank.prepare_coverage"


async def _active_jobs(db: AsyncSession, job_ids: list[str] | None) -> list[tuple[str, CanonicalJob]]:
    filters = ["item_type = 'JOB_DESCRIPTION'", "listing_status = 'ACTIVE'", "structured_data IS NOT NULL"]
    params: dict[str, Any] = {}
    if job_ids:
        filters.append("id = ANY(:job_ids)")
        params["job_ids"] = list(job_ids)
    rows = await db.execute(
        text(
            "SELECT id, structured_data, active_version_id FROM job_descriptions WHERE "
            + " AND ".join(filters)
            + " ORDER BY id"
        ),
        params,
    )
    jobs = []
    for row in rows.mappings().all():
        try:
            parsed = CanonicalJobDescription.model_validate(row["structured_data"])
            jobs.append(
                (
                    str(row["id"]),
                    canonical_job_from_description(
                        parsed,
                        job_id=str(row["id"]),
                        job_version_id=str(row["active_version_id"]) if row["active_version_id"] else None,
                    ),
                )
            )
        except (ValidationError, ValueError) as exc:
            logger.warning("Skipping job %s for question coverage: %s", row["id"], exc)
    return jobs


def _must_have_targets(job: CanonicalJob) -> list[dict[str, Any]]:
    targets: dict[str, dict[str, Any]] = {}
    for requirement in job.requirements:
        if getattr(requirement, "priority", None) != "must_have":
            continue
        for concept in _requirement_concepts(requirement):
            targets.setdefault(
                concept.concept_id,
                {
                    "taxonomyVersion": concept.taxonomy_version,
                    "conceptId": concept.concept_id,
                    "label": concept.label or concept.concept_id,
                    "rationale": {"source": "job_requirement"},
                },
            )
    return list(targets.values())


async def prepare_job_question_coverage(
    db: AsyncSession, *, job_ids: list[str] | None = None
) -> dict[str, Any]:
    """Make every must-have skill of the ACTIVE jobs reach MIN_QUESTIONS_PER_SKILL questions.

    Idempotent: a skill that is already covered costs no LLM call, so this is
    safe to run after every publish and as a backfill over all jobs.
    """
    summary = {"jobs": 0, "skills": 0, "alreadyCovered": 0, "generated": 0, "stillShort": []}
    if not get_settings().question_generation_enabled:
        summary["skipped"] = "question generation disabled"
        return summary
    done: set[tuple[str, str]] = set()
    for job_id, job in await _active_jobs(db, job_ids):
        summary["jobs"] += 1
        difficulty = derive_difficulty(job)["level"]
        job_concepts, job_role = await _job_taxonomy(db, job_id)
        for target in _must_have_targets(job):
            key = (target["conceptId"], difficulty)
            if key in done:
                continue
            done.add(key)
            summary["skills"] += 1
            filed = await ensure_generated_questions(
                db,
                target=target,
                job_concepts=job_concepts,
                job_role=job_role,
                difficulty=difficulty,
                locale=PREGENERATION_LOCALE,
                desired=MIN_QUESTIONS_PER_SKILL,
                max_count=MAX_PREGENERATED_PER_TARGET,
            )
            summary["generated"] += filed
            reachable = len(
                await reachable_candidates(
                    db,
                    target=target,
                    locale=PREGENERATION_LOCALE,
                    difficulty=difficulty,
                    job_concepts=job_concepts,
                    job_role=job_role,
                    needed=MIN_QUESTIONS_PER_SKILL,
                )
            )
            if filed == 0 and reachable >= MIN_QUESTIONS_PER_SKILL:
                summary["alreadyCovered"] += 1
            elif reachable < MIN_QUESTIONS_PER_SKILL:
                summary["stillShort"].append({"conceptId": target["conceptId"], "reachable": reachable})
    return summary


def enqueue_question_coverage(job_ids: list[str] | None = None) -> bool:
    """Ask the worker to prepare coverage; never fails the caller (HTTP request, ingest run)."""
    settings = get_settings()
    if not settings.question_generation_enabled or not settings.question_pregeneration_enabled:
        return False
    try:
        from src.workers.celery_app import celery_app

        celery_app.send_task(PREPARE_TASK_NAME, args=[{"job_ids": list(job_ids) if job_ids else None}])
        return True
    except Exception as exc:  # broker down: the next publish or a backfill catches up
        logger.warning("Could not enqueue question coverage for %s: %s", job_ids, exc)
        return False


# ── Coverage report ─────────────────────────────────────────────────────────


def _pct(part: int, whole: int) -> float:
    return round(100 * part / whole, 1) if whole else 0.0


async def question_coverage_report(db: AsyncSession) -> dict[str, Any]:
    """Coverage of locked interview sessions and of the ACTIVE jobs' must-have skills."""
    rows = await db.execute(
        text(
            """
            SELECT s.duration_minutes, p.plan_payload -> 'questionSelection' AS selection
            FROM interview_session_plans p
            JOIN interview_sessions s ON s.id = p.session_id
            WHERE p.status = 'LOCKED' AND p.plan_payload ? 'questionSelection'
            """
        )
    )
    groups: dict[str, dict[str, Any]] = {}
    for row in rows.mappings().all():
        selection = row["selection"] or {}
        group = "demo" if (row["duration_minutes"] or 0) <= DEMO_DURATION_MINUTES else "regular"
        stats = groups.setdefault(
            group,
            {
                "sessions": 0,
                "fullyReviewed": 0,
                "withGenerated": 0,
                "withUncovered": 0,
                "questionSources": {},
            },
        )
        stats["sessions"] += 1
        generated = int(selection.get("generatedQuestionCount") or 0)
        uncovered = len(selection.get("uncoveredTargets") or [])
        stats["withGenerated"] += generated > 0
        stats["withUncovered"] += uncovered > 0
        stats["fullyReviewed"] += generated == 0 and uncovered == 0
        for source, count in (selection.get("questionSources") or {}).items():
            stats["questionSources"][source] = stats["questionSources"].get(source, 0) + int(count)
    for stats in groups.values():
        total = stats["sessions"]
        stats["fullyReviewedPct"] = _pct(stats["fullyReviewed"], total)
        stats["withGeneratedPct"] = _pct(stats["withGenerated"], total)
        stats["withUncoveredPct"] = _pct(stats["withUncovered"], total)

    skills = {"total": 0, "reviewedOnly": 0, "withGenerated": 0, "short": 0}
    jobs = []
    for job_id, job in await _active_jobs(db, None):
        difficulty = derive_difficulty(job)["level"]
        job_concepts, job_role = await _job_taxonomy(db, job_id)
        job_short = []
        for target in _must_have_targets(job):
            found = await reachable_candidates(
                db,
                target=target,
                locale=PREGENERATION_LOCALE,
                difficulty=difficulty,
                job_concepts=job_concepts,
                job_role=job_role,
                needed=MIN_QUESTIONS_PER_SKILL,
            )
            skills["total"] += 1
            if len(found) < MIN_QUESTIONS_PER_SKILL:
                skills["short"] += 1
                job_short.append(target["conceptId"])
            elif any(item.source == "generated_unreviewed" for item in found[:MIN_QUESTIONS_PER_SKILL]):
                skills["withGenerated"] += 1
            else:
                skills["reviewedOnly"] += 1
        jobs.append({"jobId": job_id, "title": job.job_title, "shortSkills": job_short})
    return {
        "minQuestionsPerSkill": MIN_QUESTIONS_PER_SKILL,
        "sessions": groups,
        "mustHaveSkills": {
            **skills,
            "reviewedOnlyPct": _pct(skills["reviewedOnly"], skills["total"]),
            "withGeneratedPct": _pct(skills["withGenerated"], skills["total"]),
            "shortPct": _pct(skills["short"], skills["total"]),
        },
        "jobs": jobs,
    }
