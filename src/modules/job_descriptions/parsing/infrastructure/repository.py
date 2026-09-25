from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.job_descriptions.domain.schemas import CanonicalJobDescription
from src.modules.job_descriptions.parsing.application.pipeline import JobDescriptionDocument


class SqlAlchemyJobDescriptionParseRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def claim(self, upload_id: str) -> JobDescriptionDocument | None:
        result = await self._session.execute(
            text(
                "UPDATE job_descriptions "
                "SET status = 'PARSING', processing_status = 'PROCESSING', "
                "error = NULL, updated_at = now() "
                "WHERE id = :id AND item_type = 'JD_UPLOAD' AND status IN ('PENDING', 'FAILED') "
                "RETURNING id, filename, storage_key, checksum"
            ),
            {"id": upload_id},
        )
        row = result.mappings().one_or_none()
        if row is None:
            await self._session.rollback()
            return None
        await self._session.commit()
        return JobDescriptionDocument(
            upload_id=row["id"],
            filename=row["filename"],
            storage_key=row["storage_key"],
            checksum=row["checksum"],
        )

    async def complete(
        self,
        document: JobDescriptionDocument,
        *,
        raw_text: str,
        parsed: CanonicalJobDescription,
        parse_source: str,
    ) -> None:
        await self._session.execute(
            text(
                "UPDATE job_descriptions "
                "SET status = 'DONE', processing_status = 'DONE', "
                "raw_text = :raw_text, "
                "description = :raw_text, structured_data = CAST(:structured_data AS jsonb), "
                "parse_source = :parse_source, updated_at = now() "
                "WHERE id = :id AND item_type = 'JD_UPLOAD'"
            ),
            {
                "id": document.upload_id,
                "raw_text": raw_text,
                "structured_data": parsed.model_dump_json(by_alias=True),
                "parse_source": parse_source,
            },
        )
        await self._session.commit()

    async def fail(self, upload_id: str, error: str) -> None:
        await self._session.execute(
            text(
                "UPDATE job_descriptions "
                "SET status = 'FAILED', processing_status = 'FAILED', "
                "error = :error, updated_at = now() "
                "WHERE id = :id AND item_type = 'JD_UPLOAD'"
            ),
            {"id": upload_id, "error": error},
        )
        await self._session.commit()
