import hashlib
import logging

from fastapi import HTTPException, status
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.trace_logging import trace_event
from src.modules.job_descriptions.schemas import CanonicalJobDescription
from src.modules.matching.domain.adapters import job_description_to_matching_job
from src.modules.matching.domain.schemas import (
    CanonicalJob,
    EvidenceSpan,
    GroundedJobText,
    UnresolvedRequirement,
)
from src.modules.user_cvs.schemas import CanonicalResume

logger = logging.getLogger(__name__)


async def resolve_resume(db: AsyncSession, cv_id: str) -> CanonicalResume:
    res = await db.execute(
        text("SELECT id, parsed_data, raw_text, status FROM user_cvs WHERE id = :id"),
        {"id": cv_id},
    )
    row = res.mappings().one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail=f"Không tìm thấy hồ sơ CV với mã {cv_id}")
    if row["parsed_data"]:
        try:
            return CanonicalResume.model_validate(row["parsed_data"]).model_copy(
                update={"raw_text": row["raw_text"] or ""}
            )
        except Exception as err:
            raise HTTPException(
                status_code=422,
                detail=f"Dữ liệu phân tích CV không hợp lệ: {err}",
            ) from err
    raise HTTPException(
        status_code=400,
        detail=f"CV '{cv_id}' chưa hoàn thành bóc tách hoặc thiếu dữ liệu phân tích.",
    )

async def resolve_job(db: AsyncSession, job_id: str) -> CanonicalJob:
    res = await db.execute(
        text(
            "SELECT id, title, keywords, description, requirements, structured_data, status, "
            "active_version_id "
            "FROM job_descriptions WHERE id = :id AND item_type = 'JOB_DESCRIPTION' "
            "AND listing_status = 'ACTIVE'"
        ),
        {"id": job_id},
    )
    row = res.mappings().one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail=f"Không tìm thấy tin tuyển dụng với mã {job_id}")

    # CANONICAL PATH — only entered when structured_data exists.
    # Invariant: if structured_data is present, we MUST use it or fail closed.
    # Falling back to the legacy synthetic path is FORBIDDEN when structured_data exists.
    active_version_id = row["active_version_id"] if "active_version_id" in row else None
    if row["structured_data"]:
        try:
            parsed_jd = CanonicalJobDescription.model_validate(row["structured_data"])
        except ValidationError as err:
            logger.warning(
                "matching.resolve_job canonical_validation_failed job_id=%s "
                "error_type=%s schema_version=%s",
                job_id,
                type(err).__name__,
                row["structured_data"].get("schemaVersion", "unknown")
                if isinstance(row["structured_data"], dict)
                else "unknown",
            )
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"structured_data của JD '{job_id}' không đúng contract "
                    f"CanonicalJobDescription — không thể tiếp tục matching. "
                    f"Lỗi: {err.error_count()} validation error(s). "
                    f"Hãy kiểm tra lại kết quả parsing của JD này."
                ),
            ) from err

        try:
            if not parsed_jd.evidence:
                logger.warning(
                    "matching.resolve_job canonical_no_evidence job_id=%s",
                    job_id,
                )
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=(
                        f"JD '{job_id}' có structured_data nhưng thiếu evidence — "
                        f"JD chưa hoàn thành parsing, không thể tiếp tục matching."
                    ),
                )
            job = job_description_to_matching_job(parsed_jd, job_id=job_id)
            trace_event(
                "matching",
                "job_resolved",
                job_id=job_id,
                parser_version=parsed_jd.parsing.parser_version,
                source_requirement_count=len(parsed_jd.requirements),
                matching_requirement_count=len(job.requirements),
                requirement_ids=[item.requirement_id for item in job.requirements],
            )
            return job.model_copy(
                update={"job_version_id": str(active_version_id) if active_version_id else None}
            )
        except HTTPException:
            raise
        except ValueError as err:
            logger.warning(
                "matching.resolve_job matching_adapter_failed job_id=%s "
                "error_type=%s error=%s",
                job_id,
                type(err).__name__,
                str(err),
            )
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"JD '{job_id}' vượt qua canonical validation nhưng "
                    f"không thể convert sang matching contract: {err}"
                ),
            ) from err
        except Exception as err:
            logger.error(
                "matching.resolve_job matching_adapter_unexpected_error job_id=%s "
                "error_type=%s",
                job_id,
                type(err).__name__,
                exc_info=True,
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=(
                    f"Lỗi không mong đợi khi xử lý JD '{job_id}'. "
                    f"Vui lòng thử lại hoặc liên hệ hỗ trợ."
                ),
            ) from err

    # LEGACY PATH — only reached when structured_data is absent (None/empty).
    # This supports JDs that have not yet been processed by the canonical parser.
    # Do NOT move this block above the structured_data guard.

    doc_id = f"doc-jd-{job_id}"
    full_text = (
        f"{row['title']}\n\n{row['description']}\n\n{row['requirements']}".strip() or "Job Description"
    )
    doc_sha256 = hashlib.sha256(full_text.encode("utf-8")).hexdigest()
    ev_id = f"ev-jd-{job_id}-1"
    ev_text = full_text[:500]
    evidence = [
        EvidenceSpan(
            evidenceId=ev_id,
            documentId=doc_id,
            documentSha256=doc_sha256,
            section="requirements",
            text=ev_text,
            charStart=0,
            charEnd=len(ev_text),
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
        jobVersionId=str(active_version_id) if active_version_id else None,
        documentId=doc_id,
        documentSha256=doc_sha256,
        jobTitle=row["title"],
        responsibilities=[
            GroundedJobText(text=row["description"][:300] or row["title"], evidenceRefs=[ev_id])
        ],
        requirements=requirements,
        evidence=evidence,
    )
