"""Change one's own password (email and password accounts only).

A Google account has no password here (``password_hash`` is NULL); it is
refused rather than given one, so it cannot gain a second way to sign in.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.security import hash_password, verify_password

MIN_LENGTH = 8


class PasswordChangeError(Exception):
    def __init__(self, status_code: int, code: str) -> None:
        super().__init__(code)
        self.status_code = status_code
        self.code = code


async def change_password(db: AsyncSession, user_id: str, current: str, new: str) -> None:
    row = (
        (
            await db.execute(
                text("SELECT password_hash FROM users WHERE id = :id AND is_active = true"),
                {"id": user_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise PasswordChangeError(404, "not_found")
    if not row["password_hash"]:
        raise PasswordChangeError(409, "no_password")
    if not verify_password(current, row["password_hash"]):
        raise PasswordChangeError(400, "wrong_password")
    if len(new) < MIN_LENGTH:
        raise PasswordChangeError(400, "too_short")
    if new == current:
        raise PasswordChangeError(400, "same_password")
    await db.execute(
        text("UPDATE users SET password_hash = :hash, updated_at = now() WHERE id = :id"),
        {"id": user_id, "hash": hash_password(new)},
    )
    await db.commit()
