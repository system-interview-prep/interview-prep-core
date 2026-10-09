"""HTTP endpoints for asking and resolving candidate clarifications."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.security import current_user
from src.core.trace_logging import trace_event
from src.infrastructure.database import get_db
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


async def _resolve_match_request(
    payload: MatchClarificationIdsRequest, db: AsyncSession, user: dict
) -> MatchRequest:
    if payload.candidate_id.startswith("sample-") or payload.job_id.startswith("sample-"):
        raise HTTPException(status_code=422, detail="Sample matches do not support clarification answers")
    resume = await resolve_resume(db, payload.candidate_id, owner_id=resume_owner_scope(user))
    job = await resolve_job(db, payload.job_id)
    return MatchRequest(
        schemaVersion="2.1",
        resume=resume,
        job=job,
        matchingPolicy=payload.matching_policy,
        candidatePreferences=payload.candidate_preferences,
        asyncProcessing=False,
    )


@router.post("/clarifications", response_model=MatchClarificationAnalysis)
async def analyze_clarifications(payload: MatchRequest) -> MatchClarificationAnalysis:
    """Return the ordinary match plus optional requests for missing factual evidence."""

    return await run_match_with_clarifications(payload)


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
    request = await _resolve_match_request(payload, db, user)
    return await run_match_with_clarifications(request)


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
    request = await _resolve_match_request(payload, db, user)
    return await prepare_match_clarifications(request)


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
    request = await _resolve_match_request(payload, db, user)
    return await generate_match_clarifications(
        request,
        set(payload.requirement_ids),
        payload.clarification_token,
    )


@router.post("/clarifications/rescore", response_model=MatchClarificationRescoreResult)
async def rescore_after_clarifications(
    payload: MatchClarificationRescoreRequest,
) -> MatchClarificationRescoreResult:
    """Recalculate the match after the candidate answers clarification questions."""

    try:
        return await rescore_match_with_clarification_answers(payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/clarifications/rescore-by-ids", response_model=MatchClarificationRescoreResult)
async def rescore_after_clarifications_by_ids(
    payload: MatchClarificationRescoreIdsRequest,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(current_user),
) -> MatchClarificationRescoreResult:
    """Resolve canonical inputs server-side and recalculate after answers."""

    request = await _resolve_match_request(payload, db, user)
    try:
        rescore_payload = MatchClarificationRescoreRequest(
            matchRequest=request,
            initialAnalysis=payload.initial_analysis,
            answers=payload.answers,
        )
        return await rescore_match_with_clarification_answers(rescore_payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
