import json
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.security import current_user
from src.infrastructure.database import get_db

router = APIRouter(prefix="/ai", tags=["scoring"])


class ScoreRequest(BaseModel):
    cvId: str = Field(min_length=1)
    jobDescriptionId: str = Field(min_length=1)


def _score_value(result: dict) -> float:
    for key in ("overall_score", "overallScore", "score", "similarity"):
        value = result.get(key)
        if isinstance(value, (int, float)):
            return max(0.0, min(1.0, float(value)))
    return 0.0


@router.post("/score-cv-jp")
async def score_cv_job(
    payload: ScoreRequest,
    user: dict = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    cv = (
        await db.execute(
            text("SELECT raw_text FROM user_cvs WHERE id = :id AND user_id = :uid AND status = 'DONE'"),
            {"id": payload.cvId, "uid": user["sub"]},
        )
    ).scalar_one_or_none()
    if not cv:
        raise HTTPException(status_code=404, detail="Parsed CV not found")
    job = (
        await db.execute(
            text(
                "SELECT COALESCE(NULLIF(raw_text, ''), description) FROM job_descriptions "
                "WHERE id = :id AND item_type = 'JOB_DESCRIPTION'"
            ),
            {"id": payload.jobDescriptionId},
        )
    ).scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job description not found")

    from fastapi.concurrency import run_in_threadpool

    from src.modules.matching.facade import get_matching_facade

    result = await run_in_threadpool(
        get_matching_facade().match,
        resume_text=cv,
        job_description=job,
        algorithms=["embedding_cosine"],
        position=None,
        cv_id=payload.cvId,
        job_description_id=payload.jobDescriptionId,
    )
    score = _score_value(result)
    output = {
        "cvId": payload.cvId,
        "jobDescriptionId": payload.jobDescriptionId,
        "score": {"raw": score, "max": 1, "percentage": round(score * 100)},
        "decision": "PASS" if score >= 0.6 else "FAIL",
        "details": result,
    }
    await db.execute(
        text(
            "INSERT INTO scoring_history (id, user_id, score, details) "
            "VALUES (:id, :uid, :score, CAST(:details AS jsonb))"
        ),
        {"id": str(uuid4()), "uid": user["sub"], "score": score, "details": json.dumps(output)},
    )
    await db.commit()
    return output
