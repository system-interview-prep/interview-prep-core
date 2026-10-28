"""Real interviews the candidate reports, with the readiness shown at the time.

The point of practising is the interview outside. The progress page claims a
round is "ready"; these reports are how that claim gets checked (research
20/10). Each report stores, next to what the candidate tells us, the
readiness rounds of the position at the moment of reporting
(``progress.round_readiness``), so readiness can later be compared with
real results. Table: migration ``20261020_0036``.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.interviews.core.interview_rounds import AREAS

STAGES = ("screening", "technical", "final", "other")
RESULTS = ("passed", "failed", "waiting")
MAX_REPORTS = 50


def readiness_snapshot(progress: dict[str, Any], job_id: str | None) -> dict[str, Any]:
    """The rounds the progress page showed for this position (or the most practised one)."""
    readiness = progress.get("readiness") or []
    entry = next((item for item in readiness if item["jobId"] == job_id), None)
    if entry is None and readiness:
        entry = max(readiness, key=lambda item: item["sessions"])
    if entry is None:
        return {}
    return {
        "jobId": entry["jobId"],
        "sessions": entry["sessions"],
        "rounds": {r["area"]: {"status": r["status"], "estimate": r["estimate"]} for r in entry["rounds"]},
    }


def _row(row: Any) -> dict[str, Any]:
    areas = row["asked_areas"]
    if isinstance(areas, str):
        areas = json.loads(areas or "[]")
    readiness = row["readiness"]
    if isinstance(readiness, str):
        readiness = json.loads(readiness or "{}")
    return {
        "id": row["id"],
        "jobId": row["job_id"],
        "company": row["company"],
        "date": row["interview_date"].isoformat(),
        "stage": row["stage"],
        "result": row["result"],
        "askedAreas": [a for a in areas or [] if a in AREAS],
        "hardParts": row["hard_parts"],
        "readiness": readiness or {},
    }


async def list_reports(db: AsyncSession, user_id: str) -> list[dict[str, Any]]:
    try:
        rows = (
            (
                await db.execute(
                    text(
                        """
                    SELECT id, job_id, company, interview_date, stage, result, asked_areas, hard_parts, readiness
                    FROM real_interview_reports WHERE user_id = :uid
                    ORDER BY interview_date DESC, created_at DESC LIMIT :limit
                    """
                    ),
                    {"uid": user_id, "limit": MAX_REPORTS},
                )
            )
            .mappings()
            .all()
        )
    except Exception:  # noqa: BLE001 - migration 20261020_0036 not applied yet
        await db.rollback()
        return []
    return [_row(row) for row in rows]


async def save_report(
    db: AsyncSession,
    user_id: str,
    *,
    report_id: str | None,
    job_id: str | None,
    company: str | None,
    interview_date: date,
    stage: str,
    result: str,
    asked_areas: list[str],
    hard_parts: str | None,
    readiness: dict[str, Any],
) -> dict[str, Any]:
    """Insert, or update the result of an earlier report (a "waiting" one usually)."""
    if stage not in STAGES or result not in RESULTS:
        raise ValueError("invalid stage or result")
    areas = [a for a in dict.fromkeys(asked_areas) if a in AREAS]
    params = {
        "uid": user_id,
        "job_id": job_id,
        "company": (company or "").strip()[:160] or None,
        "date": interview_date,
        "stage": stage,
        "result": result,
        "areas": json.dumps(areas),
        "hard": (hard_parts or "").strip()[:2000] or None,
    }
    if report_id:
        updated = await db.execute(
            text(
                """
                UPDATE real_interview_reports
                SET job_id = :job_id, company = :company, interview_date = :date, stage = :stage,
                    result = :result, asked_areas = CAST(:areas AS jsonb), hard_parts = :hard, updated_at = now()
                WHERE id = :id AND user_id = :uid
                """
            ),
            {**params, "id": report_id},
        )
        if not updated.rowcount:
            raise LookupError("report not found")
        new_id = report_id
    else:
        new_id = str(uuid4())
        await db.execute(
            text(
                """
                INSERT INTO real_interview_reports
                (id, user_id, job_id, company, interview_date, stage, result, asked_areas, hard_parts, readiness)
                VALUES (:id, :uid, :job_id, :company, :date, :stage, :result, CAST(:areas AS jsonb), :hard,
                        CAST(:readiness AS jsonb))
                """
            ),
            {**params, "id": new_id, "readiness": json.dumps(readiness, ensure_ascii=False)},
        )
    await db.commit()
    reports = await list_reports(db, user_id)
    return next(item for item in reports if item["id"] == new_id)


async def delete_report(db: AsyncSession, user_id: str, report_id: str) -> bool:
    deleted = await db.execute(
        text("DELETE FROM real_interview_reports WHERE id = :id AND user_id = :uid"),
        {"id": report_id, "uid": user_id},
    )
    await db.commit()
    return bool(deleted.rowcount)
