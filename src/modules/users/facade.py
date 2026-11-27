"""Public contract of the users module for other modules.

Other modules ask here instead of joining the ``users`` table themselves, so
the account's name has one definition (trimmed, empty means none).
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

__all__ = ["account_display_name"]


async def account_display_name(db: AsyncSession, user_id: str | None) -> str | None:
    """The name on the account, or None when it has none or does not exist.

    Used by the interview to address the candidate (product owner's call,
    2026-11-27: the account's name, not the one read from a CV).
    """
    if not user_id:
        return None
    value = await db.scalar(
        text("SELECT NULLIF(BTRIM(name), '') FROM users WHERE id = :user_id"),
        {"user_id": str(user_id)},
    )
    name = str(value or "").strip()
    return name or None
