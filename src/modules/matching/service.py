from fastapi.concurrency import run_in_threadpool

from src.modules.matching.facade import get_matching_facade
from src.modules.matching.schemas import MatchRequest, MatchResult


async def run_match(payload: MatchRequest) -> MatchResult:
    """Explicit threadpool path for local/debug calls; production uses Celery."""
    return await run_in_threadpool(get_matching_facade().match, payload)
