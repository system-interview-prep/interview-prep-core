from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from fastapi.responses import Response
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.security import current_user
from src.infrastructure.database import get_db
from src.infrastructure.r2 import delete_object, get_object, public_url, put_object

router = APIRouter(prefix="/users/me/cvs", tags=["user-cvs"])

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


def _cv(row: dict) -> dict:
    return {
        "id": row["id"],
        "userId": row["user_id"],
        "originalName": row["filename"],
        "filename": row["filename"],
        "contentType": row["content_type"],
        "size": row["size"],
        "s3Key": row["s3_key"],
        "storageKey": row["s3_key"],
        "url": row["url"],
        "status": row["status"],
        "score": row["score"],
        "error": row["error"],
        "parseSource": row["parse_source"],
        "rawText": row["raw_text"],
        "createdAt": row["created_at"].isoformat(),
        "updatedAt": row["updated_at"].isoformat(),
    }


_SELECT = """
    SELECT id, user_id, filename, content_type, size, s3_key, url, status, score,
           error, parse_source, raw_text, created_at, updated_at
    FROM user_cvs
"""


@router.post("", status_code=status.HTTP_201_CREATED)
async def upload_cv(
    user: dict = Depends(current_user), db: AsyncSession = Depends(get_db), file: UploadFile = File(...)
) -> dict:
    filename = Path(file.filename or "").name
    suffix = Path(filename).suffix.lower()
    if not filename or (file.content_type not in _ALLOWED_CONTENT_TYPES and suffix not in _ALLOWED_SUFFIXES):
        raise HTTPException(status_code=422, detail="Only PDF, DOC, DOCX, PNG, JPEG, and WEBP files are allowed")
    content = await file.read(_MAX_FILE_SIZE + 1)
    if not content:
        raise HTTPException(status_code=422, detail="File is empty")
    if len(content) > _MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail="File must not exceed 10 MB")

    checksum = sha256(content).hexdigest()
    existing = await db.execute(
        text(_SELECT + " WHERE user_id = :user_id AND checksum = :checksum"),
        {"user_id": user["sub"], "checksum": checksum},
    )
    existing_row = existing.mappings().one_or_none()
    if existing_row:
        return _cv(existing_row)

    cv_id = str(uuid4())
    storage_key = f"cvs/{user['sub']}/{cv_id}{suffix}"
    try:
        put_object(storage_key, content, file.content_type or "application/octet-stream")
        await db.execute(
            text("""
                INSERT INTO user_cvs (id, user_id, checksum, filename, content_type, size, s3_key, url, status)
                VALUES (:id, :user_id, :checksum, :filename, :content_type, :size, :s3_key, :url, 'PENDING')
            """),
            {"id": cv_id, "user_id": user["sub"], "checksum": checksum, "filename": filename,
             "content_type": file.content_type or "application/octet-stream", "size": len(content),
             "s3_key": storage_key, "url": public_url(storage_key) or f"/users/me/cvs/{cv_id}/download"},
        )
        await db.commit()
    except IntegrityError:
        await db.rollback()
        delete_object(storage_key)
        raise HTTPException(status_code=409, detail="This CV has already been uploaded") from None
    from src.workers.celery_app import celery_app

    celery_app.send_task("cv.parse", args=[{"cv_id": cv_id}])
    return await _get(db, user["sub"], cv_id)


async def _get(db: AsyncSession, user_id: str, cv_id: str) -> dict:
    result = await db.execute(
        text(_SELECT + " WHERE id = :id AND user_id = :user_id"), {"id": cv_id, "user_id": user_id}
    )
    row = result.mappings().one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="CV not found")
    return _cv(row)


@router.get("")
async def list_cvs(
    user: dict = Depends(current_user),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=100),
) -> dict:
    result = await db.execute(
        text(_SELECT + " WHERE user_id = :user_id ORDER BY created_at DESC, id DESC LIMIT :limit"),
        {"user_id": user["sub"], "limit": limit},
    )
    return {"items": [_cv(row) for row in result.mappings().all()]}


@router.get("/{cv_id}")
async def get_cv(cv_id: str, user: dict = Depends(current_user), db: AsyncSession = Depends(get_db)) -> dict:
    return await _get(db, user["sub"], cv_id)


@router.get("/{cv_id}/download")
async def download_cv(cv_id: str, user: dict = Depends(current_user), db: AsyncSession = Depends(get_db)) -> Response:
    cv = await _get(db, user["sub"], cv_id)
    try:
        content = get_object(cv["s3Key"])
    except Exception as exc:
        raise HTTPException(status_code=404, detail="CV file not found") from exc
    return Response(content, media_type=cv["contentType"], headers={"Content-Disposition": f'attachment; filename="{cv["filename"]}"'})


@router.delete("/{cv_id}")
async def delete_cv(cv_id: str, user: dict = Depends(current_user), db: AsyncSession = Depends(get_db)) -> dict:
    cv = await _get(db, user["sub"], cv_id)
    result = await db.execute(
        text("DELETE FROM user_cvs WHERE id = :id AND user_id = :user_id"),
        {"id": cv_id, "user_id": user["sub"]},
    )
    await db.commit()
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="CV not found")
    try:
        delete_object(cv["s3Key"])
    except Exception:
        pass
    return {"success": True}
