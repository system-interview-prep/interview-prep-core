"""Persist and read the authenticated candidate's latest CV–JD fit per job."""

import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.matching.domain.schemas import MatchResult

logger = logging.getLogger(__name__)


def fit_counts(result: MatchResult) -> tuple[int, int]:
    applicable = [item for item in result.requirement_results if item.status != "not_applicable"]
    return sum(item.status == "met" for item in applicable), len(applicable)


async def save_latest_fit(db: AsyncSession, *, user_id: str, result: MatchResult) -> bool:
    """Upsert a summary only; never persist CV text or requirement evidence."""
    met, total = fit_counts(result)
    if total == 0:
        return False

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
    return True


async def persist_candidate_fit(db: AsyncSession, *, user: dict, result: MatchResult) -> None:
    """Save candidate-owned assessments without letting persistence break scoring."""
    if "ADMIN" in (user.get("roles") or []):
        return
    try:
        await save_latest_fit(db, user_id=str(user["sub"]), result=result)
    except Exception:  # noqa: BLE001 - fit persistence must not fail a completed assessment
        await db.rollback()
        logger.exception("Could not persist candidate CV–JD fit history")


async def list_latest_fits(db: AsyncSession, *, user_id: str) -> list[dict[str, object]]:
    result = await db.execute(
        text(
            "SELECT fit.job_id, fit.cv_id, cv.filename AS cv_name, fit.met, fit.total, fit.analyzed_at "
            "FROM candidate_job_fit_history AS fit "
            "JOIN user_cvs AS cv ON cv.id = fit.cv_id AND cv.user_id = fit.user_id "
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
        }
        for row in result.mappings().all()
    ]
