import base64
from binascii import Error as Base64Error
from hashlib import sha256
import json
from datetime import datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.security import current_user
from src.infrastructure.database import get_db
from src.infrastructure.r2 import delete_object, get_object, public_url, put_object

router = APIRouter(prefix="/admin/job-profiles", tags=["job-profiles"])

_MAX_FILE_SIZE = 10 * 1024 * 1024
_ALLOWED_CONTENT_TYPES = {
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "image/png",
    "image/jpeg",
    "image/webp",
}
_ALLOWED_SUFFIXES = {".pdf", ".doc", ".docx", ".png", ".jpg", ".jpeg", ".webp"}


class JobProfilePatch(BaseModel):
    description: str | None = None


def _json_value(value: object | None) -> str | None:
    if value is None or isinstance(value, str):
        return value
    return json.dumps(value)


def _profile(row: dict) -> dict:
    category = None
    if row["category_id"] and row["category_name"]:
        category = {
            "id": row["category_id"],
            "name": row["category_name"],
            "description": row["category_description"],
        }
    return {
        "id": row["id"],
        "title": row["title"],
        "categoryId": row["category_id"],
        "category": category,
        "keywords": row["keywords"] or [],
        "description": row["description"],
        "aiProfileUiJson": _json_value(row["ai_profile_ui_json"]),
        "aiExtrasJson": _json_value(row["ai_extras_json"]),
        "rawJdText": row["raw_jd_text"],
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
        "rawText": row["raw_jd_text"],
        "description": row["description"],
        "error": row["error"],
        "createdAt": row["created_at"].isoformat(),
        "updatedAt": row["updated_at"].isoformat(),
    }


_SELECT = """
    SELECT jp.id, jp.category_id, jp.title, jp.keywords, jp.description,
           jp.ai_profile_ui_json, jp.ai_extras_json, jp.raw_jd_text, jp.status,
           jp.created_at, jp.updated_at, jc.name AS category_name,
           jc.description AS category_description
    FROM job_profiles AS jp
    LEFT JOIN job_categories AS jc ON jc.id = jp.category_id
"""

_UPLOAD_SELECT = """
    SELECT id, owner_user_id, filename, content_type, size, status, parse_source,
           raw_jd_text, description, error, created_at, updated_at
    FROM job_profiles
"""


async def _get(db: AsyncSession, profile_id: str) -> dict:
    result = await db.execute(text(_SELECT + " WHERE jp.id = :id"), {"id": profile_id})
    row = result.mappings().one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Job profile not found")
    return _profile(row)


async def _get_upload(db: AsyncSession, user_id: str, upload_id: str) -> dict:
    result = await db.execute(
        text(_UPLOAD_SELECT + " WHERE id = :id AND owner_user_id = :user_id AND item_type = 'JP_UPLOAD'"),
        {"id": upload_id, "user_id": user_id},
    )
    row = result.mappings().one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="JD upload not found")
    return _upload(row)


def _decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        value = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
        return datetime.fromisoformat(value["createdAt"]), str(value["id"])
    except (Base64Error, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=422, detail="Invalid cursor") from exc


def _encode_cursor(profile: dict) -> str:
    value = json.dumps({"createdAt": profile["createdAt"], "id": profile["id"]}).encode()
    return base64.urlsafe_b64encode(value).decode()


@router.post("/uploads", status_code=status.HTTP_201_CREATED)
async def upload_jd(
    user: dict = Depends(current_user), db: AsyncSession = Depends(get_db), file: UploadFile = File(...)
) -> dict:
    filename = Path(file.filename or "").name
    suffix = Path(filename).suffix.lower()
    if not filename or (file.content_type not in _ALLOWED_CONTENT_TYPES and suffix not in _ALLOWED_SUFFIXES):
        raise HTTPException(
            status_code=422, detail="Only PDF, DOC, DOCX, PNG, JPEG, and WEBP files are allowed"
        )
    content = await file.read(_MAX_FILE_SIZE + 1)
    if not content:
        raise HTTPException(status_code=422, detail="File is empty")
    if len(content) > _MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail="File must not exceed 10 MB")

    checksum = sha256(content).hexdigest()
    existing = await db.execute(
        text(
            _UPLOAD_SELECT
            + " WHERE owner_user_id = :user_id AND checksum = :checksum AND item_type = 'JP_UPLOAD'"
        ),
        {"user_id": user["sub"], "checksum": checksum},
    )
    existing_row = existing.mappings().one_or_none()
    if existing_row:
        return _upload(existing_row)

    upload_id = str(uuid4())
    storage_key = f"job-profiles/{user['sub']}/{upload_id}{suffix}"
    try:
        put_object(storage_key, content, file.content_type or "application/octet-stream")
        await db.execute(
            text(
                "INSERT INTO job_profiles (id, owner_user_id, item_type, filename, content_type, size, "
                "s3_key, url, checksum, status) VALUES (:id, :user_id, 'JP_UPLOAD', :filename, "
                ":content_type, :size, :s3_key, :url, :checksum, 'PENDING')"
            ),
            {
                "id": upload_id,
                "user_id": user["sub"],
                "filename": filename,
                "content_type": file.content_type or "application/octet-stream",
                "size": len(content),
                "s3_key": storage_key,
                "url": public_url(storage_key) or f"/admin/job-profiles/uploads/{upload_id}/download",
                "checksum": checksum,
            },
        )
        await db.commit()
    except IntegrityError:
        await db.rollback()
        delete_object(storage_key)
        raise HTTPException(status_code=409, detail="This JD has already been uploaded") from None

    from src.workers.celery_app import celery_app

    celery_app.send_task("job_profile.parse", args=[{"upload_id": upload_id}])
    return await _get_upload(db, user["sub"], upload_id)


@router.get("/uploads/{upload_id}")
async def get_upload(
    upload_id: str, user: dict = Depends(current_user), db: AsyncSession = Depends(get_db)
) -> dict:
    return await _get_upload(db, user["sub"], upload_id)


@router.get("/uploads/{upload_id}/download")
async def download_upload(
    upload_id: str, user: dict = Depends(current_user), db: AsyncSession = Depends(get_db)
) -> Response:
    upload = await _get_upload(db, user["sub"], upload_id)
    result = await db.execute(text("SELECT s3_key FROM job_profiles WHERE id = :id"), {"id": upload_id})
    storage_key = result.scalar_one_or_none()
    if not storage_key:
        raise HTTPException(status_code=404, detail="JD file not found")
    try:
        content = get_object(storage_key)
    except Exception as exc:
        raise HTTPException(status_code=404, detail="JD file not found") from exc
    return Response(content, media_type=upload["contentType"], headers={"Content-Disposition": f'attachment; filename="{upload["filename"]}"'})


@router.get("")
async def list_profiles(
    _: dict = Depends(current_user),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=12, ge=1, le=100),
    cursor: str | None = None,
    category_id: str | None = Query(default=None, alias="categoryId"),
    category: str | None = None,
    q: str | None = None,
    order: Literal["asc", "desc"] = "desc",
) -> dict:
    filters = ["jp.item_type = 'JOBPROFILE'"]
    params: dict[str, object] = {"limit": limit + 1}
    if category_id:
        filters.append("jp.category_id = :category_id")
        params["category_id"] = category_id
    elif category:
        filters.append("lower(jc.name) = lower(:category)")
        params["category"] = category
    if q and q.strip():
        filters.append(
            "(jp.title ILIKE :query OR jp.description ILIKE :query OR jp.search_text ILIKE :query)"
        )
        params["query"] = f"%{q.strip()}%"
    if cursor:
        cursor_created_at, cursor_id = _decode_cursor(cursor)
        comparator = ">" if order == "asc" else "<"
        filters.append(f"(jp.created_at, jp.id) {comparator} (:cursor_created_at, :cursor_id)")
        params["cursor_created_at"] = cursor_created_at
        params["cursor_id"] = cursor_id

    result = await db.execute(
        text(
            _SELECT
            + " WHERE "
            + " AND ".join(filters)
            + f" ORDER BY jp.created_at {order.upper()}, jp.id {order.upper()} LIMIT :limit"
        ),
        params,
    )
    items = [_profile(row) for row in result.mappings().all()]
    next_cursor = _encode_cursor(items.pop()) if len(items) > limit else None
    return {"items": items, "nextCursor": next_cursor}


@router.get("/{profile_id}")
async def get_profile(
    profile_id: str, _: dict = Depends(current_user), db: AsyncSession = Depends(get_db)
) -> dict:
    return await _get(db, profile_id)


@router.patch("/{profile_id}")
async def update_profile(
    profile_id: str,
    payload: JobProfilePatch,
    _: dict = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    if "description" in payload.model_fields_set:
        result = await db.execute(
            text("UPDATE job_profiles SET description = :description, updated_at = now() WHERE id = :id"),
            {"id": profile_id, "description": payload.description or ""},
        )
        await db.commit()
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="Job profile not found")
    return await _get(db, profile_id)


@router.delete("/{profile_id}")
async def delete_profile(
    profile_id: str, _: dict = Depends(current_user), db: AsyncSession = Depends(get_db)
) -> dict:
    result = await db.execute(text("DELETE FROM job_profiles WHERE id = :id"), {"id": profile_id})
    await db.commit()
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Job profile not found")
    return {"message": "Deleted"}
