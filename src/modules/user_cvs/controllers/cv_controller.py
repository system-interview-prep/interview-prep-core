import logging

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.security import current_user
from src.infrastructure.database import get_db
from src.modules.user_cvs.application.query_service import CvContentUnavailableError, CvNotFoundError, CvQueryService
from src.modules.user_cvs.application.upload_service import (
    MAX_CV_FILE_SIZE,
    CvFileTooLarge,
    CvPersistenceUnavailable,
    CvQueueUnavailable,
    CvStorageUnavailable,
    CvUploadService,
    DuplicateCvError,
    InvalidCvFile,
)
from src.modules.user_cvs.infrastructure.messaging.celery_publisher import CeleryCvParsePublisher
from src.modules.user_cvs.infrastructure.repositories.cv_repository import SqlAlchemyCvRepository
from src.modules.user_cvs.infrastructure.repositories.upload_repository import SqlAlchemyCvUploadRepository
from src.modules.user_cvs.infrastructure.storage.cv_content_storage import R2CvContentStorage
from src.modules.user_cvs.infrastructure.storage.upload_storage import R2CvFileStorage
from src.modules.user_cvs.parsing.domain.classification import TAXONOMY_VERSION, career_taxonomy

router = APIRouter(prefix="/users/me/cvs", tags=["user-cvs"])
logger = logging.getLogger(__name__)


def _query_service(db: AsyncSession) -> CvQueryService:
    return CvQueryService(SqlAlchemyCvRepository(db), R2CvContentStorage())


def _not_found() -> HTTPException:
    return HTTPException(status_code=404, detail="CV not found")


@router.post("", status_code=status.HTTP_201_CREATED)
async def upload_cv(
    user: dict = Depends(current_user), db: AsyncSession = Depends(get_db), file: UploadFile = File(...)
) -> dict:
    content = await file.read(MAX_CV_FILE_SIZE + 1)
    service = CvUploadService(
        repository=SqlAlchemyCvUploadRepository(db),
        storage=R2CvFileStorage(),
        publisher=CeleryCvParsePublisher(),
    )
    try:
        result = await service.upload(
            user_id=user["sub"], filename=file.filename or "", content_type=file.content_type, content=content
        )
    except CvFileTooLarge as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except InvalidCvFile as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except DuplicateCvError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except CvStorageUnavailable as exc:
        logger.exception("CV upload failed while writing object storage")
        raise HTTPException(status_code=503, detail="CV storage is unavailable") from exc
    except CvPersistenceUnavailable as exc:
        logger.exception("CV upload failed while persisting metadata")
        raise HTTPException(status_code=503, detail="CV database is unavailable") from exc
    except CvQueueUnavailable as exc:
        logger.exception("CV upload persisted but parse dispatch failed")
        raise HTTPException(status_code=503, detail="CV parsing queue is unavailable") from exc
    try:
        return (await _query_service(db).get(user["sub"], result.cv_id)).as_response()
    except CvNotFoundError as exc:
        raise _not_found() from exc


@router.get("")
async def list_cvs(
    user: dict = Depends(current_user),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=100),
    career_code: str | None = Query(default=None, alias="careerCode", min_length=1),
) -> dict:
    records = await _query_service(db).list(user["sub"], limit, career_code)
    return {"items": [record.as_response() for record in records]}


@router.get("/career-taxonomy")
async def list_career_taxonomy(_: dict = Depends(current_user)) -> dict:
    return {
        "taxonomyVersion": TAXONOMY_VERSION,
        "items": [node.as_dict() for node in career_taxonomy()],
    }


@router.get("/{cv_id}")
async def get_cv(cv_id: str, user: dict = Depends(current_user), db: AsyncSession = Depends(get_db)) -> dict:
    try:
        return (await _query_service(db).get(user["sub"], cv_id)).as_response()
    except CvNotFoundError as exc:
        raise _not_found() from exc


@router.get("/{cv_id}/career-classifications")
async def get_career_classifications(
    cv_id: str, user: dict = Depends(current_user), db: AsyncSession = Depends(get_db)
) -> dict:
    try:
        cv = await _query_service(db).get(user["sub"], cv_id)
    except CvNotFoundError as exc:
        raise _not_found() from exc
    parsed_data = cv.parsed_data or {}
    return {
        "cvId": cv.id,
        "taxonomyVersion": TAXONOMY_VERSION,
        "items": parsed_data.get("careerClassifications", []),
    }


@router.get("/{cv_id}/download")
async def download_cv(
    cv_id: str, user: dict = Depends(current_user), db: AsyncSession = Depends(get_db)
) -> Response:
    try:
        cv, content = await _query_service(db).download(user["sub"], cv_id)
    except CvNotFoundError as exc:
        raise _not_found() from exc
    except CvContentUnavailableError as exc:
        raise HTTPException(status_code=404, detail="CV file not found") from exc
    return Response(
        content,
        media_type=cv.content_type,
        headers={"Content-Disposition": f'attachment; filename="{cv.filename}"'},
    )


@router.delete("/{cv_id}")
async def delete_cv(cv_id: str, user: dict = Depends(current_user), db: AsyncSession = Depends(get_db)) -> dict:
    try:
        await _query_service(db).delete(user["sub"], cv_id)
    except CvNotFoundError as exc:
        raise _not_found() from exc
    return {"success": True}
