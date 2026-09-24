"""Post-match clarification orchestration.

This module keeps candidate clarification separate from the core matching
contract. The ordinary ``/match`` path is unchanged; callers opt into this
analysis only when they want guidance for resolving unknown requirements.
"""

from __future__ import annotations

from functools import lru_cache

from fastapi.concurrency import run_in_threadpool
from pydantic import Field

from src.modules.matching.ambiguity import (
    AmbiguityAnalyzer,
    ClarificationRequest,
    build_ambiguity_analyzer_from_env,
    build_clarification_requests,
)
from src.modules.matching.facade import get_matching_facade
from src.modules.matching.schemas import MatchRequest, MatchResult
from src.modules.user_cvs.schemas import CanonicalModel


class MatchClarificationAnalysis(CanonicalModel):
    """Original match plus optional requests for additional factual evidence."""

    match_result: MatchResult
    clarification_requests: list[ClarificationRequest] = Field(default_factory=list)


@lru_cache
def get_ambiguity_analyzer() -> AmbiguityAnalyzer:
    """Reuse the analyzer/client across requests, matching other provider factories."""

    return build_ambiguity_analyzer_from_env()


async def run_match_with_clarifications(payload: MatchRequest) -> MatchClarificationAnalysis:
    """Run matching first, then analyze only the requirements left ``unknown``.

    The core result is created before the Jev call. Clarification analysis reads
    that result but never mutates it, so Jev cannot change requirement status,
    eligibility, suitability score, score provenance, or fit band.
    """

    result = await run_in_threadpool(get_matching_facade().match, payload)
    requests = await run_in_threadpool(
        build_clarification_requests,
        payload,
        result,
        get_ambiguity_analyzer(),
    )
    return MatchClarificationAnalysis(
        matchResult=result,
        clarificationRequests=requests,
    )
