"""Write notifications for people, not only for admins (admin_notifier covers AI alerts).

Every insert carries a ``dedupe_key`` (migration 0031), so retrying a request
never notifies anyone twice. Callers write inside their own transaction and
commit with the change the notification is about.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_ROW = (
    "md5(:dedupe_key || ':' || u.id)::uuid::text, u.id, :type, :title, :message, "
    "CAST(:data AS JSONB), :dedupe_key"
)
_CONFLICT = "ON CONFLICT (user_id, dedupe_key) WHERE dedupe_key IS NOT NULL DO NOTHING"


def _params(type_: str, title: str, message: str, data: dict[str, Any], dedupe_key: str) -> dict[str, Any]:
    return {
        "type": type_,
        "title": title,
        "message": message,
        "data": json.dumps(data, ensure_ascii=False),
        "dedupe_key": dedupe_key,
    }


async def notify_user(
    db: AsyncSession,
    user_id: str,
    *,
    type_: str,
    title: str,
    message: str,
    data: dict[str, Any],
    dedupe_key: str,
) -> None:
    await db.execute(
        text(
            f"INSERT INTO notifications (id, user_id, type, title, message, data, dedupe_key) "
            f"SELECT {_ROW} FROM users u WHERE CAST(u.id AS TEXT) = :user_id AND u.is_active {_CONFLICT}"
        ),
        {**_params(type_, title, message, data, dedupe_key), "user_id": str(user_id)},
    )


async def notify_role(
    db: AsyncSession,
    role: str,
    *,
    type_: str,
    title: str,
    message: str,
    data: dict[str, Any],
    dedupe_key: str,
    exclude_user_id: str | None = None,
) -> None:
    """Every active user holding ``role``, except ``exclude_user_id`` (e.g. the author)."""
    await db.execute(
        text(
            f"INSERT INTO notifications (id, user_id, type, title, message, data, dedupe_key) "
            f"SELECT {_ROW} FROM users u WHERE u.is_active AND CAST(u.id AS TEXT) <> :exclude "
            "AND EXISTS (SELECT 1 FROM user_role_assignments r WHERE r.user_id = u.id AND r.role = :role) "
            f"{_CONFLICT}"
        ),
        {**_params(type_, title, message, data, dedupe_key), "role": role, "exclude": exclude_user_id or ""},
    )
