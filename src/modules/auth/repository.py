from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


class AuthRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get_by_email(self, email: str) -> dict | None:
        result = await self.db.execute(
            text(
                "SELECT id, email, password_hash, name, role, provider, avatar_url, google_id, is_active "
                "FROM users WHERE lower(email) = :email"
            ),
            {"email": email},
        )
        row = result.mappings().one_or_none()
        return dict(row) if row else None

    async def create_local(self, *, name: str, email: str, password_hash: str, phone: str | None) -> dict:
        user_id = str(uuid4())
        await self.db.execute(
            text(
                "INSERT INTO users "
                "(id, email, password_hash, name, phone, role, provider, is_active) "
                "VALUES (:id, :email, :password_hash, :name, :phone, 'CANDIDATE', 'local', true)"
            ),
            {
                "id": user_id,
                "email": email,
                "password_hash": password_hash,
                "name": name,
                "phone": phone,
            },
        )
        await self._create_initial_credits(user_id)
        return {
            "id": user_id,
            "email": email,
            "name": name,
            "role": "CANDIDATE",
            "provider": "local",
            "avatar_url": None,
        }

    async def create_google(self, *, email: str, name: str, google_id: str, avatar_url: str | None) -> dict:
        user_id = str(uuid4())
        await self.db.execute(
            text(
                "INSERT INTO users "
                "(id, email, password_hash, name, role, provider, avatar_url, google_id, is_active) "
                "VALUES (:id, :email, NULL, :name, 'CANDIDATE', 'google', :avatar_url, :google_id, true)"
            ),
            {
                "id": user_id,
                "email": email,
                "name": name,
                "avatar_url": avatar_url,
                "google_id": google_id,
            },
        )
        await self._create_initial_credits(user_id)
        return {
            "id": user_id,
            "email": email,
            "name": name,
            "role": "CANDIDATE",
            "provider": "google",
            "avatar_url": avatar_url,
            "google_id": google_id,
            "is_active": True,
        }

    async def link_google_identity(self, user_id: str, *, google_id: str, avatar_url: str | None) -> None:
        await self.db.execute(
            text(
                "UPDATE users SET google_id = COALESCE(google_id, :google_id), "
                "avatar_url = COALESCE(:avatar_url, avatar_url), updated_at = now() WHERE id = :id"
            ),
            {"id": user_id, "google_id": google_id, "avatar_url": avatar_url},
        )

    async def _create_initial_credits(self, user_id: str) -> None:
        await self.db.execute(
            text(
                "INSERT INTO user_credits "
                "(id, user_id, cv_scans_remaining, voice_mock_remaining, plan_tier) "
                "VALUES (:id, :user_id, 3, 1, 'FREE')"
            ),
            {"id": str(uuid4()), "user_id": user_id},
        )

    async def commit(self) -> None:
        await self.db.commit()

    async def rollback(self) -> None:
        await self.db.rollback()
