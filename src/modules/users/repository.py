from datetime import date

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


class UserRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get_profile(self, user_id: str) -> dict | None:
        result = await self.db.execute(
            text(
                "SELECT u.id, u.email, u.name, u.role, u.provider, u.dob, u.avatar_url, "
                "u.created_at, c.cv_scans_remaining, c.voice_mock_remaining "
                "FROM users u LEFT JOIN user_credits c ON c.user_id = u.id "
                "WHERE u.id = :id AND u.is_active = true"
            ),
            {"id": user_id},
        )
        row = result.mappings().one_or_none()
        return dict(row) if row else None

    async def update_profile(self, user_id: str, *, name: str | None, dob: date | None) -> None:
        await self.db.execute(
            text(
                "UPDATE users SET name = COALESCE(:name, name), "
                "dob = COALESCE(CAST(:dob AS date), dob), updated_at = now() "
                "WHERE id = :id AND is_active = true"
            ),
            {"id": user_id, "name": name, "dob": dob},
        )

    async def commit(self) -> None:
        await self.db.commit()
