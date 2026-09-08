from fastapi.concurrency import run_in_threadpool

from src.modules.matching.facade import get_matching_facade
from src.modules.matching.schemas import MatchRequest, StructuredMatchRequest, StructuredMatchResult


async def run_match(payload: MatchRequest) -> dict:
    """Explicit threadpool path for local/debug calls; production uses Celery."""
    facade = get_matching_facade()
    return await run_in_threadpool(
        facade.match,
        resume_text=payload.resume_text,
        job_description=payload.job_description,
        algorithms=payload.algorithms,
        position=payload.position,
        job_description_id=payload.job_description_id,
        cv_id=payload.cv_id,
    )


async def run_structured_match(payload: StructuredMatchRequest) -> StructuredMatchResult:
    return await run_in_threadpool(get_matching_facade().match_structured, payload)
