from fastapi import APIRouter, Response, status

from src.modules.matching.schemas import (
    MatchAccepted,
    MatchRequest,
    MatchResult,
)
from src.modules.matching.service import run_match
from src.workers.tasks.matching import match_cv_to_jd

router = APIRouter(prefix="/api/v1/matching", tags=["matching"])


@router.post(
    "/match",
    response_model=MatchAccepted | MatchResult,
    responses={
        status.HTTP_202_ACCEPTED: {
            "model": MatchAccepted,
            "description": "Matching job accepted for background processing",
        }
    },
)
async def match(payload: MatchRequest, response: Response) -> MatchAccepted | MatchResult:
    if payload.async_processing:
        task = match_cv_to_jd.delay(payload.model_dump(by_alias=True))
        response.status_code = status.HTTP_202_ACCEPTED
        return MatchAccepted(taskId=task.id)
    result = await run_match(payload)
    response.status_code = status.HTTP_200_OK
    return result
