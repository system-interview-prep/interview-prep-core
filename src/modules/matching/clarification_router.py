from fastapi import APIRouter

from src.modules.matching.clarification import (
    MatchClarificationAnalysis,
    run_match_with_clarifications,
)
from src.modules.matching.schemas import MatchRequest

router = APIRouter(prefix="/api/v1/matching", tags=["matching"])


@router.post("/clarifications", response_model=MatchClarificationAnalysis)
async def analyze_clarifications(payload: MatchRequest) -> MatchClarificationAnalysis:
    """Return the ordinary match plus optional requests for missing factual evidence."""

    return await run_match_with_clarifications(payload)
