from sqlalchemy.ext.asyncio import AsyncSession

from src.core.admin_audit import record_admin_action


async def log_admin_action(
    db: AsyncSession, admin: dict, action: str, *, session_id: str | None = None,
    target_id: str | None = None, detail: dict | None = None,
) -> None:
    """Record an admin action; failures never prevent the requested action."""
    await record_admin_action(db, admin, action, session_id=session_id, target_id=target_id, detail=detail)
