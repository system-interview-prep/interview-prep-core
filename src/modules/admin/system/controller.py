from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.security import require_admin
from src.infrastructure.database import get_db
from src.modules.admin.system import system_config, system_health, user_activity
from src.modules.admin.shared.audit import log_admin_action as _log_admin_action

router = APIRouter(tags=["admin"])

@router.get("/system/config")
async def get_admin_system_config(_: dict = Depends(require_admin)) -> dict:
    """The configuration the backend runs with; read-only, secrets reported as set / not set."""
    return {"success": True, **system_config.config_view()}

@router.get("/system/health")
async def get_admin_system_health(
    _: dict = Depends(require_admin), db: AsyncSession = Depends(get_db)
) -> dict:
    """Database, RabbitMQ queues and workers, LiveKit, voice agent, Langfuse; each check times out alone."""
    return {"success": True, **await system_health.health_view(db)}

@router.get("/users/{user_id}/activity")
async def get_admin_user_activity(
    user_id: str,
    admin: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """One account: CVs, sessions with scores, matching runs, real-interview reports."""
    view = await user_activity.user_activity(db, user_id[:36])
    if view is None:
        raise HTTPException(status_code=404, detail="User not found")
    await _log_admin_action(db, admin, "VIEW_USER_ACTIVITY", target_id=user_id[:36])
    return {"success": True, **view}
