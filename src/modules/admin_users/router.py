from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession
from src.core.admin_audit import record_admin_action
from src.core.security import require_admin
from src.infrastructure.database import get_db
from src.modules.admin_users.schemas import CreateAdminUserRequest, ReplaceRolesRequest, UserStatusRequest
from src.modules.admin_users.service import AdminUserService
router=APIRouter(prefix="/admin/users",tags=["admin-users"])
@router.get("")
async def list_users(
    query: str | None = None,
    role: str | None = None,
    active: bool | None = None,
    cursor: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await AdminUserService(db).list_users(query, role, active, cursor, limit)
@router.get("/{user_id}")
async def get_user(user_id: str, _: dict = Depends(require_admin), db: AsyncSession = Depends(get_db)) -> dict:
    return await AdminUserService(db).get_user(user_id)
@router.post("",status_code=status.HTTP_201_CREATED)
async def create_user(payload:CreateAdminUserRequest,actor:dict=Depends(require_admin),db:AsyncSession=Depends(get_db))->dict:
    created = await AdminUserService(db).create(payload,actor["sub"])
    await record_admin_action(db, actor, "CREATE_USER", target_id=created.get("id"), detail={"roles": payload.roles})
    return created
@router.put("/{user_id}/roles")
async def replace_roles(user_id:str,payload:ReplaceRolesRequest,actor:dict=Depends(require_admin),db:AsyncSession=Depends(get_db))->dict:
    result = await AdminUserService(db).replace_roles(user_id,payload.roles,actor["sub"])
    await record_admin_action(db, actor, "CHANGE_ROLES", target_id=user_id, detail={"roles": payload.roles})
    return result
@router.patch("/{user_id}/status")
async def set_status(user_id:str,payload:UserStatusRequest,actor:dict=Depends(require_admin),db:AsyncSession=Depends(get_db))->dict:
    result = await AdminUserService(db).set_status(user_id,payload.isActive,actor["sub"])
    await record_admin_action(db, actor, "SET_USER_STATUS", target_id=user_id, detail={"active": payload.isActive})
    return result
