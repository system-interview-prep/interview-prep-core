"""Idempotent bootstrap data created after the database schema is ready."""

import logging
from collections.abc import Callable
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import Settings, get_settings
from src.core.security import hash_password
from src.modules.auth.schemas import PASSWORD_PATTERN

logger = logging.getLogger(__name__)


async def initialize_data(
    session_factory: Callable[[], AsyncSession],
    settings: Settings | None = None,
) -> bool:
    """Create the configured bootstrap administrator once.

    Returns ``True`` only when a new user row was created. Existing accounts
    keep their password and profile, but receive the ADMIN assignment if it is
    missing so a clean database can always be recovered deterministically.
    """
    settings = settings or get_settings()
    email = (settings.bootstrap_admin_email or "").strip().lower()
    password = settings.bootstrap_admin_password or ""
    if not email or not password:
        logger.info("Bootstrap administrator skipped: configuration is absent.")
        return False
    if not PASSWORD_PATTERN.fullmatch(password):
        raise ValueError(
            "bootstrap_admin_password must be 8-128 characters and include lowercase, "
            "uppercase, and numeric characters."
        )

    async with session_factory() as session:
        try:
            existing_id = (
                await session.execute(
                    text("SELECT id FROM users WHERE lower(email) = :email"),
                    {"email": email},
                )
            ).scalar_one_or_none()
            created = existing_id is None
            user_id = str(existing_id) if existing_id else str(uuid4())

            if created:
                await session.execute(
                    text(
                        "INSERT INTO users (id, email, password_hash, name, provider, is_active) "
                        "VALUES (:id, :email, :password_hash, :name, 'local', true)"
                    ),
                    {
                        "id": user_id,
                        "email": email,
                        "password_hash": hash_password(password),
                        "name": "System Administrator",
                    },
                )

            await session.execute(
                text(
                    "INSERT INTO user_role_assignments (user_id, role) VALUES (:user_id, 'ADMIN') "
                    "ON CONFLICT (user_id, role) DO NOTHING"
                ),
                {"user_id": user_id},
            )
            await session.execute(
                text(
                    "INSERT INTO user_credits (id, user_id, cv_scans_remaining, voice_mock_remaining, plan_tier) "
                    "VALUES (:id, :user_id, 3, 1, 'FREE') ON CONFLICT (user_id) DO NOTHING"
                ),
                {"id": str(uuid4()), "user_id": user_id},
            )
            await session.commit()
        except Exception:
            await session.rollback()
            raise

    logger.info("Bootstrap administrator %s: %s", "created" if created else "verified", email)
    return created
