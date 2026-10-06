"""Question selection and generation orchestration after the initial match."""

from functools import lru_cache

from fastapi.concurrency import run_in_threadpool

from src.modules.matching.application.facade import get_matching_facade
from src.modules.matching.clarifications.ambiguity import (
    AmbiguityAnalyzer,
    build_ambiguity_analyzer_from_env,
    build_clarification_requests,
)
from src.modules.matching.clarifications.models import MatchClarificationAnalysis
from src.modules.matching.clarifications.question_generation import (
    ClarificationQuestionService,
    build_clarification_question_service_from_env,
)
from src.modules.matching.domain.schemas import MatchRequest


@lru_cache
def get_ambiguity_analyzer() -> AmbiguityAnalyzer:
    """Reuse the analyzer/client across requests, matching other provider factories."""

    return build_ambiguity_analyzer_from_env()


@lru_cache
def get_clarification_question_service() -> ClarificationQuestionService | None:
    return build_clarification_question_service_from_env()


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
        get_clarification_question_service(),
    )
    return MatchClarificationAnalysis(
        matchResult=result,
        clarificationRequests=requests,
    )


