import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.security import current_user
from src.infrastructure.database import get_db
from src.modules.matching.application import run_tracking
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
async def match(
    payload: MatchRequest,
    response: Response,
    user: dict = Depends(current_user),
) -> MatchAccepted | MatchResult:
    if resume_owner_scope(user) is not None:
        # The resume here is client-supplied, so its documentId is not proven to
        # be the caller's CV. A stored-CV BM25 lookup by that id would let a user
        # probe another user's CV text one keyword at a time; score the text
        # that was sent instead. /match-ids resolves the CV with owner scoping.
        payload = payload.model_copy(
            update={
                "matching_policy": payload.matching_policy.model_copy(
                    update={"bm25_provider_mode": "in_memory"}
                )
            }
        )
    with run_tracking.match_run(
        "match",
        user_id=user.get("sub"),
        resume_id=payload.resume.resume_id,
        job_id=payload.job.job_id,
    ) as run:
        run_tracking.emit(
            "match_requested",
            **run_tracking.request_fields(
                "match", payload.matching_policy, is_async=payload.async_processing
            ),
        )
        # The CV and JD came in the request body: nothing to load.
        run_tracking.emit(
            "inputs_resolved", duration_ms=0, **run_tracking.inputs_fields(payload.resume, payload.job)
        )
        if payload.async_processing:
            serialized = payload.model_dump(by_alias=True)
            serialized["resume"]["rawText"] = payload.resume.raw_text or ""
            return _enqueue(serialized, run, response)
        result = await run_match(payload)
    response.status_code = status.HTTP_200_OK
    return result


def _enqueue(serialized: dict, run, response: Response) -> MatchAccepted:
    """Hand the match to Celery; the worker continues the same run (flow ``celery``)."""
    serialized[run_tracking.CELERY_RUN_KEY] = run_tracking.celery_run_payload(run)
    try:
        task = match_cv_to_jd.delay(serialized)
    except Exception as exc:
        run_tracking.emit_match_failed(exc, error_type="enqueue_failed")
        raise
    run_tracking.emit("match_enqueued", event_status="info", task_id=str(task.id))
    response.status_code = status.HTTP_202_ACCEPTED
    return MatchAccepted(taskId=task.id)


async def _handle_match_ids(
    payload: MatchIdsRequest,
    response: Response,
    db: AsyncSession,
    user: dict,
    *,
    endpoint: str = "match-ids",
) -> MatchAccepted | MatchResult:
    cv_id = payload.resolved_cv_id
    job_id = payload.resolved_job_id
    if not cv_id or not job_id:
        raise HTTPException(status_code=422, detail="Cần cung cấp candidateId (hoặc cvId) và jobId")

    # Support sample context for demo / walkthrough
    if cv_id.startswith("sample-") or job_id.startswith("sample-"):
        response.status_code = status.HTTP_200_OK
        return _sample_match_result(cv_id, job_id)

    with run_tracking.match_run("match", user_id=user.get("sub"), resume_id=cv_id, job_id=job_id) as run:
        run_tracking.emit(
            "match_requested",
            **run_tracking.request_fields(
                endpoint, payload.matching_policy, is_async=payload.async_processing
            ),
        )
        return await _match_ids_in_run(payload, response, db, user, run, cv_id, job_id)


async def _match_ids_in_run(
    payload: MatchIdsRequest,
    response: Response,
    db: AsyncSession,
    user: dict,
    run,
    cv_id: str,
    job_id: str,
) -> MatchAccepted | MatchResult:
    started = time.perf_counter()
    which = "resume"
    try:
        resume = await _resolve_resume(db, cv_id, owner_id=resume_owner_scope(user))
        which = "job"
        job = await _resolve_job(db, job_id)
    except Exception as exc:
        http_status, reason = run_tracking.input_failure_reason(which, exc)
        run_tracking.emit(
            "inputs_failed",
            http_status=http_status,
            reason=reason,
            error_type=type(exc).__name__,
            duration_ms=run_tracking.elapsed_ms(started),
        )
        run_tracking.emit_match_failed(exc, error_type="inputs_failed")
        raise
    run_tracking.emit(
        "inputs_resolved",
        duration_ms=run_tracking.elapsed_ms(started),
        **run_tracking.inputs_fields(resume, job),
    )

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
        return _enqueue(serialized, run, response)
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
    return await _handle_match_ids(payload, response, db, user, endpoint="score-cv-jp")


router = APIRouter()
router.include_router(api_router)
router.include_router(legacy_router)
