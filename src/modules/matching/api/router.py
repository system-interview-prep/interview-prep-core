import logging

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.security import current_user
from src.infrastructure.database import get_db
from src.modules.matching.application.input_resolver import resolve_job as _resolve_job
from src.modules.matching.application.input_resolver import resolve_resume as _resolve_resume
from src.modules.matching.application.input_resolver import resume_owner_scope
from src.modules.matching.application.service import run_match
from src.modules.matching.domain.schemas import (
    CandidatePreferences,
    CompatibilityResult,
    FactorResult,
    MatchAccepted,
    MatchingPolicy,
    MatchRequest,
    MatchResult,
    RequirementResult,
)
from src.workers.tasks.matching import match_cv_to_jd

logger = logging.getLogger(__name__)


class MatchIdsRequest(BaseModel):
    candidate_id: str | None = Field(default=None, alias="candidateId")
    job_id: str | None = Field(default=None, alias="jobId")
    cv_id: str | None = Field(default=None, alias="cvId")
    job_description_id: str | None = Field(default=None, alias="jobDescriptionId")
    matching_policy: MatchingPolicy = Field(default_factory=MatchingPolicy, alias="matchingPolicy")
    candidate_preferences: CandidatePreferences = Field(
        default_factory=CandidatePreferences, alias="candidatePreferences"
    )
    async_processing: bool = Field(default=False, alias="asyncProcessing")

    @property
    def resolved_cv_id(self) -> str:
        return (self.candidate_id or self.cv_id or "").strip()

    @property
    def resolved_job_id(self) -> str:
        return (self.job_id or self.job_description_id or "").strip()






def _sample_match_result(cv_id: str, job_id: str) -> MatchResult:
    return MatchResult(
        schema_version="2.1",
        pipeline_version="one-to-one-evidence-fusion-v1",
        resume_id=cv_id,
        job_id=job_id,
        policy_version="balanced-v1",
        eligibility="eligible",
        compatibility_status="compatible",
        suitability_score=0.78,
        fit_band="strong_fit",
        decision="assessed",
        requirement_results=[
            RequirementResult(
                requirement_id="req-sql-advanced",
                status="met",
                score=0.92,
                confidence=0.95,
                evidence_refs=["sample-ev-1"],
                reason_code="skill_claim_verified",
            ),
            RequirementResult(
                requirement_id="req-core-domain",
                status="met",
                score=0.84,
                confidence=0.90,
                evidence_refs=["sample-ev-2"],
                reason_code="skill_claim_verified",
            ),
        ],
        compatibility_results=[
            CompatibilityResult(
                criterion="work_mode",
                status="compatible",
                confidence=0.95,
                reason_code="candidate_accepts_hybrid",
            ),
        ],
        factor_results=[
            FactorResult(
                factor="skill",
                status="scored",
                raw_score=0.88,
                reliability=0.95,
                policy_weight=0.3,
                effective_weight=0.3,
                evidence_refs=["sample-ev-1"],
            ),
            FactorResult(
                factor="experience",
                status="scored",
                raw_score=0.82,
                reliability=0.90,
                policy_weight=0.3,
                effective_weight=0.3,
                evidence_refs=["sample-ev-2"],
            ),
            FactorResult(
                factor="semantic",
                status="scored",
                raw_score=0.76,
                reliability=0.92,
                policy_weight=0.4,
                effective_weight=0.4,
                dense_score=0.79,
                sparse_score=0.72,
                evidence_refs=["sample-ev-1", "sample-ev-2"],
            ),
        ],
        warnings=[],
    )


# Every matching call spends embedding/LLM budget and may read a stored CV:
# none of it is anonymous.
api_router = APIRouter(
    prefix="/api/v1/matching", tags=["matching"], dependencies=[Depends(current_user)]
)
legacy_router = APIRouter(prefix="/ai", tags=["matching-legacy"], dependencies=[Depends(current_user)])


@api_router.post(
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
        serialized = payload.model_dump(by_alias=True)
        serialized["resume"]["rawText"] = payload.resume.raw_text or ""
        task = match_cv_to_jd.delay(serialized)
        response.status_code = status.HTTP_202_ACCEPTED
        return MatchAccepted(taskId=task.id)
    result = await run_match(payload)
    response.status_code = status.HTTP_200_OK
    return result


async def _handle_match_ids(
    payload: MatchIdsRequest,
    response: Response,
    db: AsyncSession,
    user: dict,
) -> MatchAccepted | MatchResult:
    cv_id = payload.resolved_cv_id
    job_id = payload.resolved_job_id
    if not cv_id or not job_id:
        raise HTTPException(status_code=422, detail="Cần cung cấp candidateId (hoặc cvId) và jobId")

    # Support sample context for demo / walkthrough
    if cv_id.startswith("sample-") or job_id.startswith("sample-"):
        response.status_code = status.HTTP_200_OK
        return _sample_match_result(cv_id, job_id)

    resume = await _resolve_resume(db, cv_id, owner_id=resume_owner_scope(user))
    job = await _resolve_job(db, job_id)

    match_req = MatchRequest(
        schemaVersion="2.1",
        resume=resume,
        job=job,
        matchingPolicy=payload.matching_policy,
        candidatePreferences=payload.candidate_preferences,
        asyncProcessing=payload.async_processing,
    )
    if payload.async_processing:
        serialized = match_req.model_dump(by_alias=True)
        # rawText is runtime-only and excluded from canonical persistence, but
        # must cross the Celery boundary for targeted reparse.
        serialized["resume"]["rawText"] = resume.raw_text or ""
        task = match_cv_to_jd.delay(serialized)
        response.status_code = status.HTTP_202_ACCEPTED
        return MatchAccepted(taskId=task.id)
    result = await run_match(match_req)
    response.status_code = status.HTTP_200_OK
    return result


@api_router.post(
    "/match-ids",
    response_model=MatchAccepted | MatchResult,
    responses={
        status.HTTP_202_ACCEPTED: {
            "model": MatchAccepted,
            "description": "Matching job accepted for background processing",
        }
    },
)
async def match_by_ids(
    payload: MatchIdsRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(current_user),
) -> MatchAccepted | MatchResult:
    return await _handle_match_ids(payload, response, db, user)


@legacy_router.post("/score-cv-jp", response_model=MatchAccepted | MatchResult)
async def legacy_score_cv_jp(
    payload: MatchIdsRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(current_user),
) -> MatchAccepted | MatchResult:
    return await _handle_match_ids(payload, response, db, user)


router = APIRouter()
router.include_router(api_router)
router.include_router(legacy_router)
