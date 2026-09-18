"""Persistence adapter for the user-CV aggregate."""

import json

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.user_cvs.domain.models import CvRecord
from src.modules.user_cvs.domain.schemas import CanonicalResume

_SELECT = """
    SELECT id, user_id, checksum, filename, content_type, size, storage_key, url, status, score,
           error, parse_source, raw_text, parsed_data, created_at, updated_at
    FROM user_cvs
"""


class SqlAlchemyCvRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_owned(self, user_id: str, cv_id: str) -> CvRecord | None:
        result = await self._session.execute(
            text(_SELECT + " WHERE id = :id AND user_id = :user_id"),
            {"id": cv_id, "user_id": user_id},
        )
        row = result.mappings().one_or_none()
        return CvRecord.from_row(row) if row else None

    async def list_owned(self, user_id: str, limit: int, career_code: str | None = None) -> list[CvRecord]:
        query = _SELECT + " WHERE user_id = :user_id"
        params: dict[str, object] = {"user_id": user_id, "limit": limit}
        if career_code:
            query += " AND parsed_data @> CAST(:career_filter AS jsonb)"
            params["career_filter"] = json.dumps({"careerClassifications": [{"code": career_code}]})
        query += " ORDER BY created_at DESC, id DESC LIMIT :limit"
        result = await self._session.execute(
            text(query),
            params,
        )
        return [CvRecord.from_row(row) for row in result.mappings().all()]

    async def delete_owned(self, user_id: str, cv_id: str) -> CvRecord | None:
        record = await self.get_owned(user_id, cv_id)
        if record is None:
            return None
        result = await self._session.execute(
            text("DELETE FROM user_cvs WHERE id = :id AND user_id = :user_id"),
            {"id": cv_id, "user_id": user_id},
        )
        if result.rowcount == 0:
            return None
        await self._session.commit()
        return record

    async def update_parsed_owned(self, user_id: str, cv_id: str, parsed: CanonicalResume) -> bool:
        result = await self._session.execute(
            text(
                "UPDATE user_cvs SET parsed_data = CAST(:parsed_data AS jsonb), updated_at = now() "
                "WHERE id = :id AND user_id = :user_id AND status = 'DONE'"
            ),
            {
                "id": cv_id,
                "user_id": user_id,
                "parsed_data": parsed.model_dump_json(by_alias=True),
            },
        )
        await self._session.commit()
        return result.rowcount > 0

    async def queue_reparse_owned(self, user_id: str, cv_id: str) -> bool:
        result = await self._session.execute(
            text(
                "UPDATE user_cvs SET status = 'PENDING', error = NULL, updated_at = now() "
                "WHERE id = :id AND user_id = :user_id AND status IN ('DONE', 'FAILED')"
            ),
            {"id": cv_id, "user_id": user_id},
        )
        await self._session.commit()
        return result.rowcount > 0

    async def mark_dispatch_failed(self, cv_id: str, error: str) -> None:
        await self._session.execute(
            text("UPDATE user_cvs SET status = 'FAILED', error = :error, updated_at = now() WHERE id = :id"),
            {"id": cv_id, "error": error},
        )
        await self._session.commit()
