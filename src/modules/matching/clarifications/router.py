"""HTTP endpoints for asking and resolving candidate clarifications.

Each request is one matching run (flows clarify_full, clarify_prepare,
clarify_questions, rescore) for the admin matching view.
"""

import time
from collections.abc import Iterator
from contextlib import contextmanager

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.security import current_user
from src.core.trace_logging import trace_event
from src.infrastructure.database import get_db
from src.modules.matching.application import run_tracking
from src.modules.matching.application.input_resolver import (
    resolve_job,
    resolve_resume,
    resume_owner_scope,
)
from src.modules.matching.clarifications.models import (
    MatchClarificationAnalysis,
    MatchClarificationIdsRequest,
    MatchClarificationQuestionsIdsRequest,
    MatchClarificationRescoreIdsRequest,
    MatchClarificationRescoreRequest,
    MatchClarificationRescoreResult,
)
from src.modules.matching.clarifications.rescore import rescore_match_with_clarification_answers
from src.modules.matching.clarifications.service import (
    generate_match_clarifications,
    prepare_match_clarifications,
    run_match_with_clarifications,
)
from src.modules.matching.domain.schemas import MatchRequest

router = APIRouter(prefix="/api/v1/matching", tags=["matching"], dependencies=[Depends(current_user)])


@contextmanager
def _clarification_run(
    flow: str,
    endpoint: str,
    *,
    user: dict,
    resume_id: str | None,
    job_id: str | None,
    policy: object,
) -> Iterator[object]:
    """Open the request's run; a raised error ends it with ``match_failed``."""
    with run_tracking.match_run(flow, user_id=user.get("sub"), resume_id=resume_id, job_id=job_id) as run:
        run_tracking.emit("match_requested", **run_tracking.request_fields(endpoint, policy, is_async=False))
        try:
            yield run
        except Exception as exc:
            run_tracking.emit_match_failed(exc)
            raise


def _inputs_from_body(request: MatchRequest) -> None:
    run_tracking.emit(
        "inputs_resolved", duration_ms=0, **run_tracking.inputs_fields(request.resume, request.job)
    )


async def _resolve_match_request(
    payload: MatchClarificationIdsRequest, db: AsyncSession, user: dict
) -> MatchRequest:
    if payload.candidate_id.startswith("sample-") or payload.job_id.startswith("sample-"):
        raise HTTPException(status_code=422, detail="Sample matches do not support clarification answers")
    started = time.perf_counter()
    which = "resume"
    try:
        resume = await resolve_resume(db, payload.candidate_id, owner_id=resume_owner_scope(user))
        which = "job"
        job = await resolve_job(db, payload.job_id)
    except Exception as exc:
        http_status, reason = run_tracking.input_failure_reason(which, exc)
        run_tracking.emit(
            "inputs_failed",
            http_status=http_status,
            reason=reason,
            error_type=type(exc).__name__,
            duration_ms=run_tracking.elapsed_ms(started),
        )
        raise
    run_tracking.emit(
        "inputs_resolved",
        duration_ms=run_tracking.elapsed_ms(started),
        **run_tracking.inputs_fields(resume, job),
    )
    return MatchRequest(
        schemaVersion="2.1",
        resume=resume,
        job=job,
        matchingPolicy=payload.matching_policy,
        candidatePreferences=payload.candidate_preferences,
        asyncProcessing=False,
    )


@router.post("/clarifications", response_model=MatchClarificationAnalysis)
async def analyze_clarifications(
    payload: MatchRequest, user: dict = Depends(current_user)
) -> MatchClarificationAnalysis:
    """Return the ordinary match plus optional requests for missing factual evidence."""

    with _clarification_run(
        "clarify_full",
        "clarifications",
        user=user,
        resume_id=payload.resume.resume_id,
        job_id=payload.job.job_id,
        policy=payload.matching_policy,
    ):
        _inputs_from_body(payload)
        analysis = await run_match_with_clarifications(payload)
        run_tracking.emit_match_completed(analysis.match_result)
    return analysis


@router.post("/clarifications-by-ids", response_model=MatchClarificationAnalysis)
async def analyze_clarifications_by_ids(
    payload: MatchClarificationIdsRequest,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(current_user),
) -> MatchClarificationAnalysis:
    """Load canonical CV/JD data and return the match with AI clarification questions."""

    trace_event(
        "clarification",
        "endpoint_requested",
        endpoint="clarifications-by-ids",
        candidate_id=payload.candidate_id,
        job_id=payload.job_id,
    )
    with _clarification_run(
        "clarify_full",
        "clarifications-by-ids",
        user=user,
        resume_id=payload.candidate_id,
        job_id=payload.job_id,
        policy=payload.matching_policy,
    ):
        request = await _resolve_match_request(payload, db, user)
        analysis = await run_match_with_clarifications(request)
        run_tracking.emit_match_completed(analysis.match_result)
    return analysis


@router.post("/clarifications/prepare-by-ids", response_model=MatchClarificationAnalysis)
async def prepare_clarifications_by_ids(
    payload: MatchClarificationIdsRequest,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(current_user),
) -> MatchClarificationAnalysis:
    """Run matching and Jev's eligibility gate before generating a question."""

    trace_event(
        "clarification",
        "endpoint_requested",
        endpoint="clarifications/prepare-by-ids",
        candidate_id=payload.candidate_id,
        job_id=payload.job_id,
    )
    with _clarification_run(
        "clarify_prepare",
        "clarifications/prepare-by-ids",
        user=user,
        resume_id=payload.candidate_id,
        job_id=payload.job_id,
        policy=payload.matching_policy,
    ):
        request = await _resolve_match_request(payload, db, user)
        analysis = await prepare_match_clarifications(request)
        run_tracking.emit_match_completed(analysis.match_result)
    return analysis


@router.post("/clarifications/questions-by-ids", response_model=MatchClarificationAnalysis)
async def generate_clarification_questions_by_ids(
    payload: MatchClarificationQuestionsIdsRequest,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(current_user),
) -> MatchClarificationAnalysis:
    """Generate questions for requirements approved by the Jev phase."""

    trace_event(
        "clarification",
        "endpoint_requested",
        endpoint="clarifications/questions-by-ids",
        candidate_id=payload.candidate_id,
        job_id=payload.job_id,
        requirement_ids=sorted(set(payload.requirement_ids)),
    )
    with _clarification_run(
        "clarify_questions",
        "clarifications/questions-by-ids",
        user=user,
        resume_id=payload.candidate_id,
        job_id=payload.job_id,
        policy=payload.matching_policy,
    ):
        request = await _resolve_match_request(payload, db, user)
        analysis = await generate_match_clarifications(
            request,
            set(payload.requirement_ids),
            payload.clarification_token,
        )
        run_tracking.emit_match_completed(analysis.match_result)
    return analysis


@router.post("/clarifications/rescore", response_model=MatchClarificationRescoreResult)
async def rescore_after_clarifications(
    payload: MatchClarificationRescoreRequest,
    user: dict = Depends(current_user),
) -> MatchClarificationRescoreResult:
    """Recalculate the match after the candidate answers clarification questions."""

    request = payload.match_request
    # The three scoring passes of one rescore share this run.
    with _clarification_run(
        "rescore",
        "clarifications/rescore",
        user=user,
        resume_id=request.resume.resume_id,
        job_id=request.job.job_id,
        policy=request.matching_policy,
    ):
        _inputs_from_body(request)
        try:
            result = await rescore_match_with_clarification_answers(payload)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        run_tracking.emit_match_completed(result.final_match_result)
    return result


@router.post("/clarifications/rescore-by-ids", response_model=MatchClarificationRescoreResult)
async def rescore_after_clarifications_by_ids(
    payload: MatchClarificationRescoreIdsRequest,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(current_user),
) -> MatchClarificationRescoreResult:
    """Resolve canonical inputs server-side and recalculate after answers."""

    with _clarification_run(
        "rescore",
        "clarifications/rescore-by-ids",
        user=user,
        resume_id=payload.candidate_id,
        job_id=payload.job_id,
        policy=payload.matching_policy,
    ):
        request = await _resolve_match_request(payload, db, user)
        try:
            rescore_payload = MatchClarificationRescoreRequest(
                matchRequest=request,
                initialAnalysis=payload.initial_analysis,
                answers=payload.answers,
            )
            result = await rescore_match_with_clarification_answers(rescore_payload)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        run_tracking.emit_match_completed(result.final_match_result)
    return result
