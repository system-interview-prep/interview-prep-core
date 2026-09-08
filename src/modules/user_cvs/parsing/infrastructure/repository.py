from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.user_cvs.parsing.application.pipeline import CvDocument
from src.modules.user_cvs.domain.schemas import ParsedResume


class SqlAlchemyCvParseRepository:
    """Owns all database state transitions for one CV parse attempt."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def claim(self, cv_id: str) -> CvDocument | None:
        result = await self._session.execute(
            text(
                "UPDATE user_cvs SET status = 'PARSING', error = NULL, updated_at = now() "
                "WHERE id = :id AND status IN ('PENDING', 'FAILED') "
                "RETURNING id, filename, storage_key, checksum"
            ),
            {"id": cv_id},
        )
        row = result.mappings().one_or_none()
        if row is None:
            await self._session.rollback()
            return None
        await self._session.commit()
        return CvDocument(
            cv_id=row["id"],
            filename=row["filename"],
            storage_key=row["storage_key"],
            checksum=row["checksum"],
        )

    async def complete(
        self,
        document: CvDocument,
        *,
        raw_text: str,
        parsed: ParsedResume,
        parse_source: str,
    ) -> None:
        await self._session.execute(
            text(
                "UPDATE user_cvs SET status = 'DONE', raw_text = :raw_text, "
                "parsed_data = CAST(:parsed_data AS jsonb), parse_source = :parse_source, "
                "updated_at = now() WHERE id = :id"
            ),
            {
                "id": document.cv_id,
                "raw_text": raw_text,
                "parsed_data": parsed.resume.model_dump_json(by_alias=True),
                "parse_source": parse_source,
            },
        )
        await self._session.commit()

    async def fail(self, cv_id: str, error: str) -> None:
        await self._session.execute(
            text(
                "UPDATE user_cvs SET status = 'FAILED', error = :error, updated_at = now() "
                "WHERE id = :id"
            ),
            {"id": cv_id, "error": error},
        )
        await self._session.commit()
