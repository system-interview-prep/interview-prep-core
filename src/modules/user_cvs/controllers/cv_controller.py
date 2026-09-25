import logging

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import get_settings
from src.core.security import current_user
from src.infrastructure.database import SessionFactory, get_db
from src.modules.documents.facade import SSE_HEADERS, status_event_stream
from src.modules.user_cvs.application.query_service import (
    CvContentUnavailableError,
    CvNotFoundError,
    CvQueryService,
)
from src.modules.user_cvs.application.review_service import (
    CvReviewConflictError,
    CvReviewNotFoundError,
    CvReviewService,
)
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
from src.modules.user_cvs.domain.schemas import CanonicalResume
from src.modules.user_cvs.infrastructure.messaging.celery_publisher import CeleryCvParsePublisher
from src.modules.user_cvs.infrastructure.repositories.cv_repository import SqlAlchemyCvRepository
from src.modules.user_cvs.infrastructure.repositories.upload_repository import SqlAlchemyCvUploadRepository
from src.modules.user_cvs.infrastructure.storage.cv_content_storage import R2CvContentStorage
from src.modules.user_cvs.infrastructure.storage.upload_storage import R2CvFileStorage
from src.modules.user_cvs.parsing.domain.classification import TAXONOMY_VERSION, career_taxonomy

router = APIRouter(prefix="/users/me/cvs", tags=["user-cvs"])
logger = logging.getLogger(__name__)


class ParsedDataPatch(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    parsed_data: CanonicalResume = Field(alias="parsedData")


class ReviewDecision(BaseModel):
    approved: bool


def _query_service(db: AsyncSession) -> CvQueryService:
    return CvQueryService(SqlAlchemyCvRepository(db), R2CvContentStorage())


def _review_service(db: AsyncSession) -> CvReviewService:
    return CvReviewService(SqlAlchemyCvRepository(db), CeleryCvParsePublisher())


def _not_found() -> HTTPException:
    return HTTPException(status_code=404, detail="CV not found")


async def _status_snapshot(user_id: str, cv_id: str, db: AsyncSession) -> dict:
    record = await SqlAlchemyCvRepository(db).get_owned(user_id, cv_id)
    if record is None:
        raise CvNotFoundError
    parsing = (record.parsed_data or {}).get("parsing") or {}
    return {
        "cvId": record.id,
        "status": record.status,
        "reviewStatus": parsing.get("status"),
        "error": record.error,
        "updatedAt": record.updated_at.isoformat(),
    }


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


@router.get("/{cv_id}/events")
async def stream_cv_status(
    cv_id: str,
    request: Request,
    user: dict = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """Stream lifecycle changes without exposing CV content or identity fields."""
    try:
        await _status_snapshot(user["sub"], cv_id, db)
    except CvNotFoundError as exc:
        raise _not_found() from exc

    async def load_status() -> dict:
        async with SessionFactory() as stream_db:
            return await _status_snapshot(user["sub"], cv_id, stream_db)

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


@router.patch("/{cv_id}/parsed-data")
async def replace_parsed_data(
    cv_id: str,
    payload: ParsedDataPatch,
    user: dict = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        parsed = await _review_service(db).replace_parsed_data(user["sub"], cv_id, payload.parsed_data)
    except CvReviewNotFoundError as exc:
        raise _not_found() from exc
    except CvReviewConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"cvId": cv_id, "parsedData": parsed.model_dump(by_alias=True)}


@router.post("/{cv_id}/review")
async def review_cv(
    cv_id: str,
    payload: ReviewDecision,
    user: dict = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        parsed = await _review_service(db).set_review_status(user["sub"], cv_id, approved=payload.approved)
    except CvReviewNotFoundError as exc:
        raise _not_found() from exc
    except CvReviewConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"cvId": cv_id, "reviewStatus": parsed.parsing.status}


@router.post("/{cv_id}/reparse", status_code=status.HTTP_202_ACCEPTED)
async def reparse_cv(
    cv_id: str,
    user: dict = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        await _review_service(db).reparse(user["sub"], cv_id)
    except CvReviewNotFoundError as exc:
        raise _not_found() from exc
    except CvReviewConflictError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"cvId": cv_id, "status": "PENDING"}


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
async def delete_cv(
    cv_id: str, user: dict = Depends(current_user), db: AsyncSession = Depends(get_db)
) -> dict:
    try:
        await _query_service(db).delete(user["sub"], cv_id)
    except CvNotFoundError as exc:
        raise _not_found() from exc
    return {"success": True}
