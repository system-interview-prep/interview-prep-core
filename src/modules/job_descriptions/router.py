import base64
import json
from binascii import Error as Base64Error
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import get_settings
from src.core.security import current_user, require_admin
from src.infrastructure.database import SessionFactory, get_db
from src.infrastructure.r2 import delete_object, get_object, public_url, put_object
from src.modules.documents.facade import (
    MAX_DOCUMENT_FILE_SIZE,
    DocumentFileTooLarge,
    DocumentFileValidator,
    InvalidDocumentFile,
)
from src.modules.documents.sse import SSE_HEADERS, status_event_stream
from src.modules.job_descriptions.domain.schemas import CanonicalJobDescription

router = APIRouter(prefix="/admin/job-profiles", tags=["job-profiles"])

_MAX_FILE_SIZE = MAX_DOCUMENT_FILE_SIZE
_file_validator = DocumentFileValidator()


class JobDescriptionPatch(BaseModel):
    description: str | None = None


class UploadPatch(BaseModel):
    structuredData: dict | None = None
    extractedMetadata: dict | None = None


class FinalizeUpload(BaseModel):
    title: str = Field(min_length=1, max_length=512)
    primaryTaxonomyConceptId: str | None = None
    categoryId: str | None = None
    keywords: list[str] = Field(default_factory=list)
    status: Literal["ACTIVE", "DRAFT", "ARCHIVED"] = "ACTIVE"
    description: str | None = None


def _job_description(row: dict) -> dict:
    taxonomy = None
    if row["primary_taxonomy_concept_id"] and row["taxonomy_label"]:
        taxonomy = {"version": row["primary_taxonomy_version"], "conceptId": row["primary_taxonomy_concept_id"], "label": row["taxonomy_label"], "kind": row["taxonomy_kind"]}
    return {
        "id": row["id"],
        "title": row["title"],
        "primaryTaxonomy": taxonomy,
        "categoryId": row["primary_taxonomy_concept_id"],
        "category": {"id": row["primary_taxonomy_concept_id"], "name": row["taxonomy_label"]} if row["primary_taxonomy_concept_id"] else None,
        "keywords": row["keywords"] or [],
        "description": row["description"],
        "structuredData": row.get("structured_data"),
        "extractedMetadata": row.get("extracted_metadata"),
        "sourceText": row["raw_text"],
        "status": row["status"],
        "createdAt": row["created_at"].isoformat(),
        "updatedAt": row["updated_at"].isoformat(),
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
        "createdAt": row["created_at"].isoformat(),
        "updatedAt": row["updated_at"].isoformat(),
    }


_SELECT = """
    SELECT jd.id, jd.primary_taxonomy_version, jd.primary_taxonomy_concept_id, jd.title, jd.keywords, jd.description,
           jd.structured_data, jd.extracted_metadata, jd.raw_text, jd.status,
           jd.created_at, jd.updated_at, tc.label AS taxonomy_label, tc.kind AS taxonomy_kind
    FROM job_descriptions AS jd
    LEFT JOIN taxonomy_concepts AS tc ON tc.taxonomy_version = jd.primary_taxonomy_version AND tc.concept_id = jd.primary_taxonomy_concept_id
"""

_UPLOAD_SELECT = """
    SELECT id, owner_user_id, filename, content_type, size, status, parse_source,
           raw_text, description, structured_data, extracted_metadata, error, created_at, updated_at
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


@router.post("/uploads", status_code=status.HTTP_201_CREATED)
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
                or f"/admin/job-profiles/uploads/{upload_id}/download",
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


@router.get("/uploads/{upload_id}")
async def get_upload(
    upload_id: str, user: dict = Depends(require_admin), db: AsyncSession = Depends(get_db)
) -> dict:
    return await _get_upload(db, user["sub"], upload_id)


@router.get("/uploads/{upload_id}/events")
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


@router.post("/uploads/{upload_id}/reparse")
async def reparse_upload(
    upload_id: str, user: dict = Depends(require_admin), db: AsyncSession = Depends(get_db)
) -> dict:
    """Queue a fresh extraction for an existing upload after parser rules change."""
    await _get_upload(db, user["sub"], upload_id)
    await db.execute(
        text(
            "UPDATE job_descriptions SET status = 'PENDING', error = NULL, updated_at = now() "
            "WHERE id = :id AND owner_user_id = :uid AND item_type = 'JD_UPLOAD'"
        ),
        {"id": upload_id, "uid": user["sub"]},
    )
    await db.commit()

    from src.workers.celery_app import celery_app

    celery_app.send_task("job_description.parse", args=[{"upload_id": upload_id}])
    return await _get_upload(db, user["sub"], upload_id)


@router.patch("/uploads/{upload_id}")
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


@router.post("/uploads/{upload_id}/finalize")
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
    concept_id = payload.primaryTaxonomyConceptId or payload.categoryId
    if not concept_id:
        raise HTTPException(status_code=422, detail="primaryTaxonomyConceptId or categoryId is required")
    taxonomy_version = (await db.execute(
        text("SELECT version FROM taxonomy_versions WHERE is_active ORDER BY priority DESC, published_at DESC LIMIT 1")
    )).scalar_one_or_none()
    taxonomy_exists = await db.execute(
        text("SELECT 1 FROM taxonomy_concepts WHERE taxonomy_version = :version AND concept_id = :id AND kind IN ('domain', 'occupation', 'job_category') AND is_active"),
        {"version": taxonomy_version, "id": concept_id}
    )
    if taxonomy_exists.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="Active taxonomy concept not found")
    keywords = [item.strip() for item in payload.keywords if item.strip()][:50]
    description = (payload.description or upload["description"] or upload["rawText"] or "").strip()
    await db.execute(
        text(
            "UPDATE job_descriptions SET item_type = 'JOB_DESCRIPTION', title = :title, "
            "primary_taxonomy_version = :taxonomy_version, primary_taxonomy_concept_id = :taxonomy_concept_id, "
            "keywords = :keywords, description = :description, status = :status, "
            "search_text = :search_text, extraction_version = '1.0', extracted_at = now(), "
            "updated_at = now() WHERE id = :id AND owner_user_id = :uid AND item_type = 'JD_UPLOAD'"
        ),
        {
            "id": upload_id,
            "uid": user["sub"],
            "title": payload.title.strip(),
            "taxonomy_version": taxonomy_version,
            "taxonomy_concept_id": concept_id,
            "keywords": keywords,
            "description": description,
            "status": payload.status,
            "search_text": " ".join([payload.title, *keywords, description]),
        },
    )
    await db.commit()
    return await _get(db, upload_id)


@router.get("/uploads/{upload_id}/download")
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


@router.get("")
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


@router.get("/{job_description_id}")
async def get_job_description(
    job_description_id: str, _: dict = Depends(current_user), db: AsyncSession = Depends(get_db)
) -> dict:
    return await _get(db, job_description_id)


@router.patch("/{job_description_id}")
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


@router.delete("/{job_description_id}")
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
