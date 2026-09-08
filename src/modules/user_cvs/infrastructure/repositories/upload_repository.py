from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.user_cvs.application.upload_service import CvUploadRecord, DuplicateCvError


class SqlAlchemyCvUploadRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find_id_by_checksum(self, user_id: str, checksum: str) -> str | None:
        result = await self._session.execute(
            text("SELECT id FROM user_cvs WHERE user_id = :user_id AND checksum = :checksum"),
            {"user_id": user_id, "checksum": checksum},
        )
        return result.scalar_one_or_none()

    async def create(self, record: CvUploadRecord) -> None:
        try:
            await self._session.execute(
                text(
                    "INSERT INTO user_cvs "
                    "(id, user_id, checksum, filename, content_type, size, storage_key, url, status) "
                    "VALUES (:id, :user_id, :checksum, :filename, :content_type, :size, "
                    ":storage_key, :url, 'PENDING')"
                ),
                {
                    "id": record.cv_id,
                    "user_id": record.user_id,
                    "checksum": record.checksum,
                    "filename": record.filename,
                    "content_type": record.content_type,
                    "size": record.size,
                    "storage_key": record.storage_key,
                    "url": record.url,
                },
            )
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            raise DuplicateCvError("This CV has already been uploaded") from exc

    async def mark_dispatch_failed(self, cv_id: str, error: str) -> None:
        await self._session.execute(
            text(
                "UPDATE user_cvs SET status = 'FAILED', error = :error, updated_at = now() "
                "WHERE id = :id"
            ),
            {"id": cv_id, "error": f"queue dispatch failed: {error}"},
        )
        await self._session.commit()
