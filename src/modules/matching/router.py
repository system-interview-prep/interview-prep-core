import hashlib
from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.infrastructure.database import get_db
from src.modules.job_descriptions.schemas import CanonicalJobDescription
from src.modules.matching.adapters import job_description_to_matching_job
from src.modules.matching.schemas import (
    CandidatePreferences,
    CanonicalJob,
    CompatibilityResult,
    EvidenceSpan,
    FactorResult,
    GroundedJobText,
    MatchAccepted,
    MatchRequest,
    MatchResult,
    MatchingPolicy,
    RequirementResult,
    UnresolvedRequirement,
)
from src.modules.matching.service import run_match
from src.modules.user_cvs.schemas import CanonicalResume
from src.workers.tasks.matching import match_cv_to_jd


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


async def _resolve_resume(db: AsyncSession, cv_id: str) -> CanonicalResume:
    res = await db.execute(
        text("SELECT id, parsed_data, raw_text, status FROM user_cvs WHERE id = :id"),
        {"id": cv_id},
    )
    row = res.mappings().one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail=f"Không tìm thấy hồ sơ CV với mã {cv_id}")
    if row["parsed_data"]:
        try:
            return CanonicalResume.model_validate(row["parsed_data"])
        except Exception as err:
            raise HTTPException(
                status_code=422,
                detail=f"Dữ liệu phân tích CV không hợp lệ: {err}",
            ) from err
    raise HTTPException(
        status_code=400,
        detail=f"CV '{cv_id}' chưa hoàn thành bóc tách hoặc thiếu dữ liệu phân tích.",
    )


async def _resolve_job(db: AsyncSession, job_id: str) -> CanonicalJob:
    res = await db.execute(
        text(
            "SELECT id, title, keywords, description, requirements, structured_data, status "
            "FROM job_descriptions WHERE id = :id"
        ),
        {"id": job_id},
    )
    row = res.mappings().one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail=f"Không tìm thấy tin tuyển dụng với mã {job_id}")

    if row["structured_data"]:
        try:
            parsed_jd = CanonicalJobDescription.model_validate(row["structured_data"])
            if parsed_jd.evidence:
                return job_description_to_matching_job(parsed_jd, job_id=job_id)
        except Exception:
            pass

    # Synthesize grounded CanonicalJob from basic fields
    doc_id = f"doc-jd-{job_id}"
    full_text = f"{row['title']}\n\n{row['description']}\n\n{row['requirements']}".strip() or "Job Description"
    doc_sha256 = hashlib.sha256(full_text.encode("utf-8")).hexdigest()
    ev_id = f"ev-jd-{job_id}-1"
    evidence = [
        EvidenceSpan(
            evidence_id=ev_id,
            document_id=doc_id,
            document_sha256=doc_sha256,
            text=full_text[:500],
            start_offset=0,
            end_offset=min(500, len(full_text)),
            section_type="requirements",
        )
    ]
    req_lines = [line.strip() for line in (row["requirements"] or "").split("\n") if line.strip()]
    if not req_lines and row["keywords"]:
        req_lines = [f"Skill: {kw}" for kw in row["keywords"]]
    if not req_lines:
        req_lines = ["Core competencies"]

    requirements = [
        UnresolvedRequirement(
            requirementId=f"req-{job_id}-{idx+1}",
            priority="must_have" if idx < 3 else "nice_to_have",
            sourceEvidenceRef=ev_id,
            type="unresolved",
            kind="skill",
            rawLabel=line.lstrip("-*•0123456789. ") or line,
        )
        for idx, line in enumerate(req_lines[:20])
    ]

    return CanonicalJob(
        schemaVersion="2.1",
        jobId=job_id,
        documentId=doc_id,
        documentSha256=doc_sha256,
        jobTitle=row["title"],
        responsibilities=[
            GroundedJobText(text=row["description"][:300] or row["title"], evidenceRefs=[ev_id])
        ],
        requirements=requirements,
        evidence=evidence,
    )


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


api_router = APIRouter(prefix="/api/v1/matching", tags=["matching"])
legacy_router = APIRouter(prefix="/ai", tags=["matching-legacy"])


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
        task = match_cv_to_jd.delay(payload.model_dump(by_alias=True))
        response.status_code = status.HTTP_202_ACCEPTED
        return MatchAccepted(taskId=task.id)
    result = await run_match(payload)
    response.status_code = status.HTTP_200_OK
    return result


async def _handle_match_ids(
    payload: MatchIdsRequest,
    response: Response,
    db: AsyncSession,
) -> MatchAccepted | MatchResult:
    cv_id = payload.resolved_cv_id
    job_id = payload.resolved_job_id
    if not cv_id or not job_id:
        raise HTTPException(status_code=422, detail="Cần cung cấp candidateId (hoặc cvId) và jobId")

    # Support sample context for demo / walkthrough
    if cv_id.startswith("sample-") or job_id.startswith("sample-"):
        response.status_code = status.HTTP_200_OK
        return _sample_match_result(cv_id, job_id)

    resume = await _resolve_resume(db, cv_id)
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
        task = match_cv_to_jd.delay(match_req.model_dump(by_alias=True))
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
) -> MatchAccepted | MatchResult:
    return await _handle_match_ids(payload, response, db)


@legacy_router.post("/score-cv-jp", response_model=MatchAccepted | MatchResult)
async def legacy_score_cv_jp(
    payload: MatchIdsRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> MatchAccepted | MatchResult:
    return await _handle_match_ids(payload, response, db)


router = APIRouter()
router.include_router(api_router)
router.include_router(legacy_router)
