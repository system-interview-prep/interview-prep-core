"""HTTP endpoints for asking and resolving candidate clarifications."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from src.infrastructure.database import get_db
from src.modules.matching.application.input_resolver import resolve_job, resolve_resume
from src.modules.matching.clarifications.models import (
    MatchClarificationAnalysis,
    MatchClarificationIdsRequest,
    MatchClarificationRescoreIdsRequest,
    MatchClarificationRescoreRequest,
    MatchClarificationRescoreResult,
)
from src.modules.matching.clarifications.rescore import rescore_match_with_clarification_answers
from src.modules.matching.clarifications.service import run_match_with_clarifications
from src.modules.matching.domain.schemas import MatchRequest

router = APIRouter(prefix="/api/v1/matching", tags=["matching"])


async def _resolve_match_request(payload: MatchClarificationIdsRequest, db: AsyncSession) -> MatchRequest:
    if payload.candidate_id.startswith("sample-") or payload.job_id.startswith("sample-"):
        raise HTTPException(status_code=422, detail="Sample matches do not support clarification answers")
    resume = await resolve_resume(db, payload.candidate_id)
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
) -> MatchClarificationAnalysis:
    """Load canonical CV/JD data and return the match with AI clarification questions."""

    request = await _resolve_match_request(payload, db)
    return await run_match_with_clarifications(request)


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
) -> MatchClarificationRescoreResult:
    """Resolve canonical inputs server-side and recalculate after answers."""

    request = await _resolve_match_request(payload, db)
    try:
        rescore_payload = MatchClarificationRescoreRequest(
            matchRequest=request,
            initialAnalysis=payload.initial_analysis,
            answers=payload.answers,
        )
        return await rescore_match_with_clarification_answers(rescore_payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
