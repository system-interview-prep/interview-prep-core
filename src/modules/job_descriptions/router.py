import base64
import json
from binascii import Error as Base64Error
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse
from uuid import uuid4

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import Response, StreamingResponse
from pydantic import AliasChoices, BaseModel, Field, ValidationError, model_validator
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import get_settings
from src.core.security import current_user, require_admin
from src.infrastructure.database import SessionFactory, get_db
from src.infrastructure.r2 import delete_object, get_object, public_url, put_object
from src.modules.documents.facade import (
    MAX_DOCUMENT_FILE_SIZE,
    SSE_HEADERS,
    DocumentFileTooLarge,
    DocumentFileValidator,
    InvalidDocumentFile,
    status_event_stream,
)
from src.modules.job_descriptions.domain.schemas import CanonicalJobDescription

_inner_router = APIRouter(tags=["job-descriptions"])

_MAX_FILE_SIZE = MAX_DOCUMENT_FILE_SIZE
_file_validator = DocumentFileValidator()


def _validate_http_url(url: str | None, field_name: str) -> None:
    """Validate that URL uses http/https scheme and has a non-empty hostname, or data:image/ for logos."""
    if url is not None:
        trimmed = url.strip()
        if field_name == "companyLogoUrl" and trimmed.startswith("data:image/"):
            return
        try:
            parsed = urlparse(trimmed)
        except Exception:
            raise ValueError(f"{field_name} must be a valid http or https URL") from None
        if parsed.scheme not in ("http", "https"):
            raise ValueError(f"{field_name} must use http:// or https:// scheme (got: {parsed.scheme!r})")
        if not parsed.netloc or not parsed.hostname:
            raise ValueError(f"{field_name} must have a valid hostname")


class JobDescriptionPatch(BaseModel):
    description: str | None = None


class UploadPatch(BaseModel):
    structuredData: dict | None = None
    extractedMetadata: dict | None = None


class FinalizeExperience(BaseModel):
    min_years: int | None = Field(
        default=None,
        ge=0,
        validation_alias=AliasChoices("minYears", "min_years"),
    )
    max_years: int | None = Field(
        default=None,
        ge=0,
        validation_alias=AliasChoices("maxYears", "max_years"),
    )

    @model_validator(mode="after")
    def validate_range(self) -> "FinalizeExperience":
        if self.min_years is not None and self.max_years is not None and self.min_years > self.max_years:
            raise ValueError("experience.minYears cannot be greater than experience.maxYears")
        return self


class FinalizeSalary(BaseModel):
    min: int | None = Field(default=None, ge=0)
    max: int | None = Field(default=None, ge=0)
    currency: str | None = None
    period: Literal["hour", "month", "year"] | None = None
    negotiable: bool | None = None

    @model_validator(mode="after")
    def validate_salary(self) -> "FinalizeSalary":
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError("salary.min cannot be greater than salary.max")
        if self.min is not None or self.max is not None:
            if not self.currency or not self.currency.strip():
                raise ValueError("salary.currency is required when numeric salary is provided")
            if not self.period:
                raise ValueError("salary.period is required when numeric salary is provided")
        return self


class FinalizeSource(BaseModel):
    type: Literal["internal_upload", "manual", "greenhouse", "lever", "company_career", "other"] = (
        "internal_upload"
    )
    key: str = "default"
    name: str | None = None
    url: str | None = None
    apply_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices("applyUrl", "apply_url"),
    )

    @model_validator(mode="after")
    def validate_source(self) -> "FinalizeSource":
        _validate_http_url(self.url, "source.url")
        _validate_http_url(self.apply_url, "source.applyUrl")
        return self


class FinalizeUpload(BaseModel):
    title: str = Field(min_length=1, max_length=512)
    company_name: str | None = Field(
        default=None,
        validation_alias=AliasChoices("companyName", "company_name"),
    )
    company_logo_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices("companyLogoUrl", "company_logo_url"),
    )
    location: str | None = None
    work_mode: Literal["remote", "hybrid", "on_site"] | None = Field(
        default=None,
        validation_alias=AliasChoices("workMode", "work_mode"),
    )
    employment_type: (
        Literal["full_time", "part_time", "internship", "contract", "temporary"] | None
    ) = Field(
        default=None,
        validation_alias=AliasChoices("employmentType", "employment_type"),
    )
    seniority: (
        Literal["intern", "fresher", "junior", "mid", "senior", "lead", "manager"] | None
    ) = None
    experience: FinalizeExperience | None = None
    salary: FinalizeSalary | None = None
    primary_taxonomy_concept_id: str = Field(
        validation_alias=AliasChoices("primaryTaxonomyConceptId", "primary_taxonomy_concept_id")
    )
    primary_taxonomy_version: str | None = Field(
        default=None,
        validation_alias=AliasChoices("primaryTaxonomyVersion", "primary_taxonomy_version"),
    )
    keywords: list[str] = Field(default_factory=list)
    description: str | None = None
    source: FinalizeSource | None = None
    external_job_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices("externalJobId", "external_job_id"),
    )
    posted_at: datetime | None = Field(
        default=None,
        validation_alias=AliasChoices("postedAt", "posted_at"),
    )
    listing_status: Literal["DRAFT", "ACTIVE"] = Field(
        default="DRAFT",
        validation_alias=AliasChoices("listingStatus", "listing_status", "status"),
    )

    @property
    def primaryTaxonomyConceptId(self) -> str:
        return self.primary_taxonomy_concept_id

    @property
    def primaryTaxonomyVersion(self) -> str | None:
        return self.primary_taxonomy_version

    @property
    def listingStatus(self) -> Literal["DRAFT", "ACTIVE"]:
        return self.listing_status

    @model_validator(mode="after")
    def validate_finalize(self) -> "FinalizeUpload":
        _validate_http_url(self.company_logo_url, "companyLogoUrl")
        source = self.source
        if source and source.type in ("greenhouse", "lever", "company_career") and self.external_job_id:
            if not source.key or source.key.strip() in ("", "default"):
                raise ValueError(
                    "sourceKey cannot be 'default', empty, or null for external sources with externalJobId"
                )
        return self


def _job_description(row: dict) -> dict:
    taxonomy = None
    if row.get("primary_taxonomy_concept_id") and row.get("taxonomy_label"):
        taxonomy = {
            "version": row.get("primary_taxonomy_version"),
            "conceptId": row.get("primary_taxonomy_concept_id"),
            "label": row.get("taxonomy_label"),
            "kind": row.get("taxonomy_kind"),
        }

    company = None
    if row.get("company_name") is not None or row.get("company_logo_url") is not None:
        company = {
            "name": row.get("company_name"),
            "logoUrl": row.get("company_logo_url"),
        }

    experience = None
    if row.get("experience_min_years") is not None or row.get("experience_max_years") is not None:
        experience = {
            "minYears": row.get("experience_min_years"),
            "maxYears": row.get("experience_max_years"),
        }

    salary = None
    if any(
        row.get(k) is not None
        for k in (
            "salary_min",
            "salary_max",
            "salary_currency",
            "salary_period",
            "salary_negotiable",
        )
    ):
        salary = {
            "min": row.get("salary_min"),
            "max": row.get("salary_max"),
            "currency": row.get("salary_currency"),
            "period": row.get("salary_period"),
            "negotiable": row.get("salary_negotiable"),
        }

    source_type = row.get("source_type") or "internal_upload"
    source = {
        "type": source_type,
        "key": row.get("source_key") or "default",
        "name": row.get("source_name")
        or ("Internal Upload" if source_type == "internal_upload" else None),
        "url": row.get("source_url"),
        "applyUrl": row.get("apply_url"),
    }

    posted_at = row["posted_at"].isoformat() if row.get("posted_at") else None

    return {
        "id": row["id"],
        "externalJobId": row.get("external_job_id"),
        "title": row["title"],
        "company": company,
        "location": row.get("location"),
        "workMode": row.get("work_mode"),
        "employmentType": row.get("employment_type"),
        "seniority": row.get("seniority"),
        "experience": experience,
        "salary": salary,
        "primaryTaxonomy": taxonomy,
        "source": source,
        "postedAt": posted_at,
        "listingStatus": row.get("listing_status") or "ACTIVE",
        "processingStatus": row.get("processing_status") or "DONE",
        "keywords": row.get("keywords") or [],
        "description": row.get("description"),
        "structuredData": row.get("structured_data"),
        "extractedMetadata": row.get("extracted_metadata"),
        "sourceText": row.get("raw_text"),
        "status": row.get("status"),
        "createdAt": row["created_at"].isoformat() if row.get("created_at") else None,
        "updatedAt": row["updated_at"].isoformat() if row.get("updated_at") else None,
    }


def _upload(row: dict) -> dict:
    return {
        "id": row["id"],
        "userId": row["owner_user_id"],
        "filename": row["filename"],
        "contentType": row["content_type"],
        "size": row["size"],
        "status": row["status"],
        "parseSource": row["parse_source"],
        "rawText": row["raw_text"],
        "description": row["description"],
        "structuredData": row.get("structured_data"),
        "extractedMetadata": row.get("extracted_metadata"),
        "error": row["error"],
        "createdAt": row["created_at"].isoformat() if row.get("created_at") else None,
        "updatedAt": row["updated_at"].isoformat() if row.get("updated_at") else None,
    }


_SELECT = """
    SELECT jd.id, jd.external_job_id, jd.title,
           jd.company_name, jd.company_logo_url,
           jd.location, jd.work_mode, jd.employment_type, jd.seniority,
           jd.experience_min_years, jd.experience_max_years,
           jd.salary_min, jd.salary_max, jd.salary_currency, jd.salary_period, jd.salary_negotiable,
           jd.primary_taxonomy_version, jd.primary_taxonomy_concept_id,
           jd.source_type, jd.source_key, jd.source_name, jd.source_url, jd.apply_url,
           jd.posted_at, jd.listing_status, jd.processing_status,
           jd.keywords, jd.description, jd.structured_data, jd.extracted_metadata, jd.raw_text, jd.status,
           jd.created_at, jd.updated_at, tc.label AS taxonomy_label, tc.kind AS taxonomy_kind
    FROM job_descriptions AS jd
    LEFT JOIN taxonomy_concepts AS tc ON tc.taxonomy_version = jd.primary_taxonomy_version AND tc.concept_id = jd.primary_taxonomy_concept_id
"""

_UPLOAD_SELECT = """
    SELECT id, owner_user_id, filename, content_type, size, status, parse_source,
           raw_text, description, structured_data, extracted_metadata, error,
           external_job_id, processing_status, listing_status,
           created_at, updated_at
    FROM job_descriptions
"""


async def _get(db: AsyncSession, job_description_id: str) -> dict:
    result = await db.execute(
        text(_SELECT + " WHERE jd.id = :id AND jd.item_type = 'JOB_DESCRIPTION'"),
        {"id": job_description_id},
    )
    row = result.mappings().one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Job description not found")
    return _job_description(row)


async def _get_upload(db: AsyncSession, user_id: str, upload_id: str) -> dict:
    result = await db.execute(
        text(_UPLOAD_SELECT + " WHERE id = :id AND owner_user_id = :user_id AND item_type = 'JD_UPLOAD'"),
        {"id": upload_id, "user_id": user_id},
    )
    row = result.mappings().one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="JD upload not found")
    return _upload(row)


async def _upload_status_snapshot(db: AsyncSession, user_id: str, upload_id: str) -> dict:
    upload = await _get_upload(db, user_id, upload_id)
    return {
        "uploadId": upload["id"],
        "status": upload["status"],
        "error": upload["error"],
        "updatedAt": upload["updatedAt"],
    }


def _decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        value = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
        return datetime.fromisoformat(value["createdAt"]), str(value["id"])
    except (Base64Error, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=422, detail="Invalid cursor") from exc


def _encode_cursor(job_description: dict) -> str:
    value = json.dumps(
        {"createdAt": job_description["createdAt"], "id": job_description["id"]}
    ).encode()
    return base64.urlsafe_b64encode(value).decode()


@_inner_router.post("/uploads", status_code=status.HTTP_201_CREATED)
async def upload_jd(
    user: dict = Depends(require_admin), db: AsyncSession = Depends(get_db), file: UploadFile = File(...)
) -> dict:
    content = await file.read(_MAX_FILE_SIZE + 1)
    try:
        filename, detected_content_type = _file_validator.validate(
            file.filename or "", file.content_type, content
        )
    except DocumentFileTooLarge as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except InvalidDocumentFile as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    suffix = Path(filename).suffix.lower()

    checksum = sha256(content).hexdigest()
    existing = await db.execute(
        text(
            _UPLOAD_SELECT
            + " WHERE owner_user_id = :user_id AND checksum = :checksum AND item_type = 'JD_UPLOAD'"
        ),
        {"user_id": user["sub"], "checksum": checksum},
    )
    existing_row = existing.mappings().one_or_none()
    if existing_row:
        return _upload(existing_row)

    upload_id = str(uuid4())
    storage_key = f"job-descriptions/{user['sub']}/{upload_id}{suffix}"
    try:
        put_object(storage_key, content, detected_content_type)
        await db.execute(
            text(
                "INSERT INTO job_descriptions (id, owner_user_id, item_type, filename, content_type, size, "
                "storage_key, url, checksum, status) VALUES (:id, :user_id, 'JD_UPLOAD', :filename, "
                ":content_type, :size, :storage_key, :url, :checksum, 'PENDING')"
            ),
            {
                "id": upload_id,
                "user_id": user["sub"],
                "filename": filename,
                "content_type": detected_content_type,
                "size": len(content),
                "storage_key": storage_key,
                "url": public_url(storage_key)
                or f"/admin/job-descriptions/uploads/{upload_id}/download",
                "checksum": checksum,
            },
        )
        await db.commit()
    except IntegrityError:
        await db.rollback()
        delete_object(storage_key)
        raise HTTPException(status_code=409, detail="This JD has already been uploaded") from None

    from src.workers.celery_app import celery_app

    celery_app.send_task("job_description.parse", args=[{"upload_id": upload_id}])
    return await _get_upload(db, user["sub"], upload_id)


@_inner_router.get("/uploads/{upload_id}")
async def get_upload(
    upload_id: str, user: dict = Depends(require_admin), db: AsyncSession = Depends(get_db)
) -> dict:
    return await _get_upload(db, user["sub"], upload_id)


@_inner_router.get("/uploads/{upload_id}/events")
async def stream_upload_status(
    upload_id: str,
    request: Request,
    user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """Stream JD-upload lifecycle changes without raw text or parsed content."""
    await _upload_status_snapshot(db, user["sub"], upload_id)

    async def load_status() -> dict:
        async with SessionFactory() as stream_db:
            return await _upload_status_snapshot(stream_db, user["sub"], upload_id)

    settings = get_settings()
    return StreamingResponse(
        status_event_stream(
            request,
            load_status,
            poll_interval_seconds=settings.sse_status_poll_interval_seconds,
            heartbeat_seconds=settings.sse_heartbeat_seconds,
        ),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


@_inner_router.post("/uploads/{upload_id}/reparse")
async def reparse_upload(
    upload_id: str, user: dict = Depends(require_admin), db: AsyncSession = Depends(get_db)
) -> dict:
    """Queue a fresh extraction for an existing upload after parser rules change."""
    await _get_upload(db, user["sub"], upload_id)
    await db.execute(
        text(
            "UPDATE job_descriptions "
            "SET status = 'PENDING', processing_status = 'PENDING', "
            "error = NULL, updated_at = now() "
            "WHERE id = :id AND owner_user_id = :uid AND item_type = 'JD_UPLOAD'"
        ),
        {"id": upload_id, "uid": user["sub"]},
    )
    await db.commit()

    from src.workers.celery_app import celery_app

    celery_app.send_task("job_description.parse", args=[{"upload_id": upload_id}])
    return await _get_upload(db, user["sub"], upload_id)


@_inner_router.patch("/uploads/{upload_id}")
async def patch_upload(
    upload_id: str,
    payload: UploadPatch,
    user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    values = payload.model_dump(exclude_unset=True)
    if "structuredData" in values and values["structuredData"] is not None:
        try:
            values["structuredData"] = CanonicalJobDescription.model_validate(
                values["structuredData"]
            ).model_dump(by_alias=True)
        except ValidationError as exc:
            raise HTTPException(
                status_code=422,
                detail="structuredData must satisfy CanonicalJobDescription schema",
            ) from exc
    if values:
        result = await db.execute(
            text(
                "UPDATE job_descriptions SET structured_data = "
                "COALESCE(CAST(:structured_data AS jsonb), structured_data), "
                "extracted_metadata = COALESCE(CAST(:metadata AS jsonb), extracted_metadata), "
                "updated_at = now() "
                "WHERE id = :id AND owner_user_id = :uid AND item_type = 'JD_UPLOAD'"
            ),
            {
                "id": upload_id,
                "uid": user["sub"],
                "structured_data": json.dumps(values["structuredData"])
                if "structuredData" in values
                else None,
                "metadata": json.dumps(values["extractedMetadata"])
                if "extractedMetadata" in values
                else None,
            },
        )
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="JD upload not found")
        await db.commit()
    return await _get_upload(db, user["sub"], upload_id)


@_inner_router.post("/uploads/{upload_id}/finalize")
async def finalize_upload(
    upload_id: str,
    payload: FinalizeUpload,
    user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    upload = await _get_upload(db, user["sub"], upload_id)
    if upload["status"] != "DONE":
        raise HTTPException(status_code=409, detail="Upload is not DONE yet")
    try:
        CanonicalJobDescription.model_validate(upload["structuredData"] or {})
    except ValidationError as exc:
        raise HTTPException(
            status_code=409,
            detail="Upload has no schema-valid structuredData; review the parsed JD first",
        ) from exc

    # External source identity validation
    effective_external_id = payload.external_job_id or upload.get("external_job_id")
    source_type = payload.source.type if payload.source else "internal_upload"
    source_key = payload.source.key if payload.source else "default"
    if source_type in ("greenhouse", "lever", "company_career") and effective_external_id:
        if not source_key or source_key.strip() in ("", "default"):
            raise HTTPException(
                status_code=422,
                detail="sourceKey cannot be 'default', empty, or null for external sources with externalJobId",
            )

    concept_id = payload.primary_taxonomy_concept_id
    if not concept_id:
        raise HTTPException(status_code=422, detail="primaryTaxonomyConceptId is required")

    taxonomy_version = payload.primary_taxonomy_version
    if not taxonomy_version:
        taxonomy_version = (
            await db.execute(
                text(
                    "SELECT version FROM taxonomy_versions WHERE is_active ORDER BY priority DESC, published_at DESC LIMIT 1"
                )
            )
        ).scalar_one_or_none()

    taxonomy_exists = await db.execute(
        text(
            "SELECT 1 FROM taxonomy_concepts WHERE taxonomy_version = :version AND concept_id = :id "
            "AND kind IN ('domain', 'occupation', 'job_family', 'competency', 'job_role', 'specialization') AND is_active"
        ),
        {"version": taxonomy_version, "id": concept_id},
    )
    if taxonomy_exists.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="Active taxonomy concept not found")

    keywords = [item.strip() for item in payload.keywords if item.strip()][:50]
    description = (payload.description or upload["description"] or upload["rawText"] or "").strip()

    # Extract source fields
    source_name = payload.source.name if payload.source else None
    if not source_name and source_type == "internal_upload":
        source_name = "Internal Upload"
    source_url = payload.source.url if payload.source else None
    apply_url = payload.source.apply_url if payload.source else None

    # Extract experience and salary fields
    exp_min = payload.experience.min_years if payload.experience else None
    exp_max = payload.experience.max_years if payload.experience else None

    sal_min = payload.salary.min if payload.salary else None
    sal_max = payload.salary.max if payload.salary else None
    sal_curr = payload.salary.currency if payload.salary else None
    sal_per = payload.salary.period if payload.salary else None
    sal_neg = payload.salary.negotiable if payload.salary else None

    listing_status = payload.listing_status
    # Legacy status: reflects listingStatus
    legacy_status = listing_status

    try:
        result = await db.execute(
            text(
                "UPDATE job_descriptions SET "
                "item_type = 'JOB_DESCRIPTION', "
                "title = :title, "
                "company_name = :company_name, "
                "company_logo_url = :company_logo_url, "
                "location = :location, "
                "work_mode = :work_mode, "
                "employment_type = :employment_type, "
                "seniority = :seniority, "
                "experience_min_years = :experience_min_years, "
                "experience_max_years = :experience_max_years, "
                "salary_min = :salary_min, "
                "salary_max = :salary_max, "
                "salary_currency = :salary_currency, "
                "salary_period = :salary_period, "
                "salary_negotiable = :salary_negotiable, "
                "primary_taxonomy_version = :taxonomy_version, "
                "primary_taxonomy_concept_id = :taxonomy_concept_id, "
                "source_type = :source_type, "
                "source_key = :source_key, "
                "source_name = :source_name, "
                "source_url = :source_url, "
                "apply_url = :apply_url, "
                "external_job_id = COALESCE(:external_job_id, external_job_id), "
                "posted_at = :posted_at, "
                "processing_status = 'DONE', "
                "listing_status = :listing_status, "
                "status = :status, "
                "keywords = :keywords, "
                "description = :description, "
                "search_text = :search_text, "
                "extraction_version = '1.0', "
                "extracted_at = now(), "
                "updated_at = now() "
                "WHERE id = :id AND owner_user_id = :uid AND item_type = 'JD_UPLOAD'"
            ),
            {
                "id": upload_id,
                "uid": user["sub"],
                "title": payload.title.strip(),
                "company_name": payload.company_name.strip() if payload.company_name else None,
                "company_logo_url": payload.company_logo_url.strip() if payload.company_logo_url else None,
                "location": payload.location.strip() if payload.location else None,
                "work_mode": payload.work_mode,
                "employment_type": payload.employment_type,
                "seniority": payload.seniority,
                "experience_min_years": exp_min,
                "experience_max_years": exp_max,
                "salary_min": sal_min,
                "salary_max": sal_max,
                "salary_currency": sal_curr,
                "salary_period": sal_per,
                "salary_negotiable": sal_neg,
                "taxonomy_version": taxonomy_version,
                "taxonomy_concept_id": concept_id,
                "source_type": source_type,
                "source_key": source_key,
                "source_name": source_name,
                "source_url": source_url,
                "apply_url": apply_url,
                "external_job_id": payload.external_job_id,
                "posted_at": payload.posted_at,
                "listing_status": listing_status,
                "status": legacy_status,
                "keywords": keywords,
                "description": description,
                "search_text": " ".join([payload.title, *keywords, description]),
            },
        )
        if result.rowcount == 0:
            raise HTTPException(status_code=409, detail="Upload has already been finalized or modified")
        await db.commit()
    except Exception:
        await db.rollback()
        raise

    return await _get(db, upload_id)


@_inner_router.get("/uploads/{upload_id}/download")
async def download_upload(
    upload_id: str, user: dict = Depends(require_admin), db: AsyncSession = Depends(get_db)
) -> Response:
    upload = await _get_upload(db, user["sub"], upload_id)
    result = await db.execute(
        text("SELECT storage_key FROM job_descriptions WHERE id = :id"), {"id": upload_id}
    )
    storage_key = result.scalar_one_or_none()
    if not storage_key:
        raise HTTPException(status_code=404, detail="JD file not found")
    try:
        content = get_object(storage_key)
    except Exception as exc:
        raise HTTPException(status_code=404, detail="JD file not found") from exc
    return Response(
        content,
        media_type=upload["contentType"],
        headers={"Content-Disposition": f'attachment; filename="{upload["filename"]}"'},
    )


@_inner_router.get("")
async def list_job_descriptions(
    _: dict = Depends(current_user),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=12, ge=1, le=100),
    cursor: str | None = None,
    taxonomy_concept_id: str | None = Query(default=None, alias="taxonomyConceptId"),
    q: str | None = None,
    order: Literal["asc", "desc"] = "desc",
) -> dict:
    filters = ["jd.item_type = 'JOB_DESCRIPTION'"]
    params: dict[str, object] = {"limit": limit + 1}
    if taxonomy_concept_id:
        filters.append("jd.primary_taxonomy_concept_id = :taxonomy_concept_id")
        params["taxonomy_concept_id"] = taxonomy_concept_id
    if q and q.strip():
        filters.append(
            "(jd.title ILIKE :query OR jd.description ILIKE :query OR jd.search_text ILIKE :query)"
        )
        params["query"] = f"%{q.strip()}%"
    if cursor:
        cursor_created_at, cursor_id = _decode_cursor(cursor)
        comparator = ">" if order == "asc" else "<"
        filters.append(f"(jd.created_at, jd.id) {comparator} (:cursor_created_at, :cursor_id)")
        params["cursor_created_at"] = cursor_created_at
        params["cursor_id"] = cursor_id

    result = await db.execute(
        text(
            _SELECT
            + " WHERE "
            + " AND ".join(filters)
            + f" ORDER BY jd.created_at {order.upper()}, jd.id {order.upper()} LIMIT :limit"
        ),
        params,
    )
    items = [_job_description(row) for row in result.mappings().all()]
    next_cursor = _encode_cursor(items.pop()) if len(items) > limit else None
    return {"items": items, "nextCursor": next_cursor}


@_inner_router.get("/{job_description_id}")
async def get_job_description(
    job_description_id: str, _: dict = Depends(current_user), db: AsyncSession = Depends(get_db)
) -> dict:
    return await _get(db, job_description_id)


@_inner_router.patch("/{job_description_id}")
async def update_job_description(
    job_description_id: str,
    payload: JobDescriptionPatch,
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    if "description" in payload.model_fields_set:
        result = await db.execute(
            text(
                "UPDATE job_descriptions SET description = :description, updated_at = now() "
                "WHERE id = :id AND item_type = 'JOB_DESCRIPTION'"
            ),
            {"id": job_description_id, "description": payload.description or ""},
        )
        await db.commit()
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="Job description not found")
    return await _get(db, job_description_id)


@_inner_router.delete("/{job_description_id}")
async def delete_job_description(
    job_description_id: str, _: dict = Depends(require_admin), db: AsyncSession = Depends(get_db)
) -> dict:
    result = await db.execute(
        text("DELETE FROM job_descriptions WHERE id = :id AND item_type = 'JOB_DESCRIPTION'"),
        {"id": job_description_id},
    )
    await db.commit()
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Job description not found")
    return {"message": "Deleted"}


router = APIRouter()
_LEGACY_PREFIX = "".join(["/admin", "/job", "-profiles"])
router.include_router(_inner_router, prefix=_LEGACY_PREFIX)
router.include_router(_inner_router, prefix="/admin/job-descriptions")

