"""Persist and read the authenticated candidate's latest CV–JD fit per job."""

import json
import logging
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.matching.domain.schemas import MatchResult

logger = logging.getLogger(__name__)


def fit_counts(result: MatchResult) -> tuple[int, int]:
    applicable = [item for item in result.requirement_results if item.status != "not_applicable"]
    return sum(item.status == "met" for item in applicable), len(applicable)


async def save_latest_fit(db: AsyncSession, *, user_id: str, result: MatchResult) -> str | None:
    """Append a reviewable result snapshot and upsert the latest fit summary."""
    met, total = fit_counts(result)
    if total == 0:
        return None
    attempt_id = uuid4()

    await db.execute(
        text(
            "INSERT INTO candidate_job_fit_attempts "
            "(id, user_id, job_id, cv_id, met, total, analyzed_at, result_snapshot) "
            "VALUES (:id, :user_id, :job_id, :cv_id, :met, :total, now(), CAST(:result_snapshot AS jsonb))"
        ),
        {
            "id": attempt_id,
            "user_id": user_id,
            "job_id": result.job_id,
            "cv_id": result.resume_id,
            "met": met,
            "total": total,
            "result_snapshot": json.dumps(result.model_dump(mode="json", by_alias=True)),
        },
    )
    await db.execute(
        text(
            "INSERT INTO candidate_job_fit_history "
            "(user_id, job_id, cv_id, met, total, analyzed_at, updated_at) "
            "VALUES (:user_id, :job_id, :cv_id, :met, :total, now(), now()) "
            "ON CONFLICT (user_id, job_id) DO UPDATE SET "
            "cv_id = EXCLUDED.cv_id, met = EXCLUDED.met, total = EXCLUDED.total, "
            "analyzed_at = now(), updated_at = now()"
        ),
        {
            "user_id": user_id,
            "job_id": result.job_id,
            "cv_id": result.resume_id,
            "met": met,
            "total": total,
        },
    )
    await db.commit()
    return str(attempt_id)


async def persist_candidate_fit(db: AsyncSession, *, user: dict, result: MatchResult) -> str | None:
    """Save candidate-owned assessments without letting persistence break scoring."""
    if "ADMIN" in (user.get("roles") or []):
        return None
    try:
        return await save_latest_fit(db, user_id=str(user["sub"]), result=result)
    except Exception:  # noqa: BLE001 - fit persistence must not fail a completed assessment
        await db.rollback()
        logger.exception("Could not persist candidate CV–JD fit history")
        return None


async def list_latest_fits(db: AsyncSession, *, user_id: str) -> list[dict[str, object]]:
    result = await db.execute(
        text(
            "SELECT fit.job_id, fit.cv_id, cv.filename AS cv_name, fit.met, fit.total, fit.analyzed_at, "
            "latest_attempt.id AS attempt_id "
            "FROM candidate_job_fit_history AS fit "
            "JOIN user_cvs AS cv ON cv.id = fit.cv_id AND cv.user_id = fit.user_id "
            "LEFT JOIN LATERAL ("
            "SELECT attempt.id FROM candidate_job_fit_attempts AS attempt "
            "WHERE attempt.user_id = fit.user_id AND attempt.job_id = fit.job_id AND attempt.cv_id = fit.cv_id "
            "ORDER BY attempt.analyzed_at DESC, attempt.id DESC LIMIT 1"
            ") AS latest_attempt ON TRUE "
            "WHERE fit.user_id = :user_id "
            "ORDER BY fit.analyzed_at DESC"
        ),
        {"user_id": user_id},
    )
    return [
        {
            "jobId": str(row["job_id"]),
            "cvId": str(row["cv_id"]),
            "cvName": row["cv_name"],
            "met": int(row["met"]),
            "total": int(row["total"]),
            "analyzedAt": row["analyzed_at"].isoformat(),
            "attemptId": str(row["attempt_id"]) if row["attempt_id"] is not None else None,
        }
        for row in result.mappings().all()
    ]


async def list_fit_attempts(
    db: AsyncSession, *, user_id: str, job_id: str, limit: int = 50
) -> list[dict[str, object]]:
    result = await db.execute(
        text(
            "SELECT attempt.id, attempt.job_id, attempt.cv_id, cv.filename AS cv_name, "
            "attempt.met, attempt.total, attempt.analyzed_at, "
            "attempt.result_snapshot IS NOT NULL AS has_snapshot "
            "FROM candidate_job_fit_attempts AS attempt "
            "LEFT JOIN user_cvs AS cv ON cv.id = attempt.cv_id AND cv.user_id = attempt.user_id "
            "WHERE attempt.user_id = :user_id AND attempt.job_id = :job_id "
            "ORDER BY attempt.analyzed_at DESC LIMIT :limit"
        ),
        {"user_id": user_id, "job_id": job_id, "limit": limit},
    )
    return [
        {
            "id": str(row["id"]),
            "jobId": str(row["job_id"]),
            "cvId": str(row["cv_id"]),
            "cvName": row["cv_name"],
            "met": int(row["met"]),
            "total": int(row["total"]),
            "analyzedAt": row["analyzed_at"].isoformat(),
            "hasSnapshot": bool(row["has_snapshot"]),
        }
        for row in result.mappings().all()
    ]


async def list_candidate_fit_attempts(
    db: AsyncSession, *, user_id: str, limit: int = 100
) -> list[dict[str, object]]:
    """Return this candidate's recent CV–JD matching history across all jobs."""
    result = await db.execute(
        text(
            "SELECT attempt.id, attempt.job_id, job.title AS job_title, attempt.cv_id, "
            "cv.filename AS cv_name, attempt.met, attempt.total, attempt.analyzed_at, "
            "attempt.result_snapshot IS NOT NULL AS has_snapshot "
            "FROM candidate_job_fit_attempts AS attempt "
            "LEFT JOIN user_cvs AS cv ON cv.id = attempt.cv_id AND cv.user_id = attempt.user_id "
            "LEFT JOIN job_descriptions AS job ON job.id = attempt.job_id "
            "WHERE attempt.user_id = :user_id "
            "ORDER BY attempt.analyzed_at DESC LIMIT :limit"
        ),
        {"user_id": user_id, "limit": limit},
    )
    return [
        {
            "id": str(row["id"]),
            "jobId": str(row["job_id"]),
            "jobTitle": row["job_title"],
            "cvId": str(row["cv_id"]),
            "cvName": row["cv_name"],
            "met": int(row["met"]),
            "total": int(row["total"]),
            "analyzedAt": row["analyzed_at"].isoformat(),
            "hasSnapshot": bool(row["has_snapshot"]),
        }
        for row in result.mappings().all()
    ]


async def get_fit_attempt(
    db: AsyncSession, *, user_id: str, attempt_id: str
) -> dict[str, object] | None:
    result = await db.execute(
        text(
            "SELECT attempt.id, attempt.job_id, attempt.cv_id, cv.filename AS cv_name, "
            "attempt.met, attempt.total, attempt.analyzed_at, attempt.result_snapshot "
            "FROM candidate_job_fit_attempts AS attempt "
            "LEFT JOIN user_cvs AS cv ON cv.id = attempt.cv_id AND cv.user_id = attempt.user_id "
            "WHERE attempt.user_id = :user_id AND attempt.id = :attempt_id"
        ),
        {"user_id": user_id, "attempt_id": attempt_id},
    )
    row = result.mappings().first()
    if row is None:
        return None
    return {
        "id": str(row["id"]),
        "jobId": str(row["job_id"]),
        "cvId": str(row["cv_id"]),
        "cvName": row["cv_name"],
        "met": int(row["met"]),
        "total": int(row["total"]),
        "analyzedAt": row["analyzed_at"].isoformat(),
        "resultSnapshot": row["result_snapshot"],
    }
