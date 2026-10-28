"""Record what an admin did, for the "Nhật ký thao tác" page.

One row per action in ``admin_access_logs`` (migration 0030): who, what action,
which session or object, and a small JSON detail. Reads of candidate data
(answers, room observation, user detail) and changes (question review and
approval, JD publishing, roles, taxonomy, grading, CV reparse) share the table,
so the page shows one timeline.

Writing the log never fails the action: it runs after the action's own commit,
and an error (table not migrated) is swallowed after a rollback.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

DETAIL_MAX_CHARS = 2000


def _detail(detail: dict[str, Any] | None) -> str:
    raw = json.dumps(detail or {}, ensure_ascii=False, default=str)
    return raw if len(raw) <= DETAIL_MAX_CHARS else json.dumps({"truncated": True})


async def record_admin_action(
    db: AsyncSession,
    actor: dict[str, Any],
    action: str,
    *,
    session_id: str | None = None,
    target_id: Any = None,
    detail: dict[str, Any] | None = None,
) -> None:
    try:
        await db.execute(
            text(
                "INSERT INTO admin_access_logs "
                "(admin_user_id, admin_email, action, session_id, target_id, detail) "
                "VALUES (:uid, :email, :action, :sid, :tid, CAST(:detail AS JSONB))"
            ),
            {
                "uid": str(actor.get("sub") or "")[:36],
                "email": actor.get("email"),
                "action": action[:64],
                "sid": (str(session_id)[:36] if session_id else None),
                "tid": (str(target_id)[:64] if target_id else None),
                "detail": _detail(detail),
            },
        )
        await db.commit()
    except Exception as exc:  # noqa: BLE001 - never fail the action over its log
        logger.warning("Admin action %s not logged: %s", action, exc)
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass
