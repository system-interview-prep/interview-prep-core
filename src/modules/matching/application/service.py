import time

from src.core.trace_logging import trace_event
from src.modules.matching.application import run_tracking
from src.modules.matching.application.facade import get_matching_facade
from src.modules.matching.application.match_cache import cached_match
from src.modules.matching.domain.schemas import MatchRequest, MatchResult


async def run_match(payload: MatchRequest) -> MatchResult:
    """Run database-backed matching in FastAPI's active event loop."""
    started_at = time.monotonic()
    trace_event("matching", "started", resume_id=payload.resume.resume_id, job_id=payload.job.job_id)
    try:
        # Same cache as the interview planner, so the page and the plan agree.
        result = await cached_match(payload, get_matching_facade().match_async)
    except Exception as exc:
        trace_event(
            "matching",
            "failed",
            resume_id=payload.resume.resume_id,
            job_id=payload.job.job_id,
            error_type=type(exc).__name__,
            duration_ms=round((time.monotonic() - started_at) * 1000),
        )
        run_tracking.emit_match_failed(exc)
        raise
    trace_event(
        "matching",
        "completed",
        resume_id=payload.resume.resume_id,
        job_id=payload.job.job_id,
        decision=result.decision,
        fit_band=result.fit_band,
        eligibility=result.eligibility,
        diagnostic_score=result.diagnostic_score,
        final_suitability_score=result.suitability_score,
        failed_must_have_requirement_ids=result.failed_must_have_requirements,
        suitability_score=result.suitability_score,
        warnings=result.warnings,
        duration_ms=round((time.monotonic() - started_at) * 1000),
    )
    run_tracking.emit_match_completed(result)
    return result
