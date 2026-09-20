from uuid import uuid4
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from src.core.security import hash_password
from src.modules.admin_users.schemas import CreateAdminUserRequest

class AdminUserService:
    def __init__(self, db: AsyncSession): self.db=db
    async def list_users(self, query: str | None, limit: int) -> list[dict]:
        rows=await self.db.execute(text("SELECT u.id,u.email,u.name,u.provider,u.is_active,u.created_at,array_agg(ura.role ORDER BY ura.role) roles FROM users u JOIN user_role_assignments ura ON ura.user_id=u.id WHERE (:q IS NULL OR lower(u.email) LIKE lower(:q) OR lower(u.name) LIKE lower(:q)) GROUP BY u.id ORDER BY u.created_at DESC LIMIT :limit"),{"q":f"%{query.strip()}%" if query and query.strip() else None,"limit":limit})
        return [dict(row) for row in rows.mappings()]
    async def create(self, payload: CreateAdminUserRequest, actor_id: str) -> dict:
        exists=await self.db.scalar(text("SELECT 1 FROM users WHERE lower(email)=lower(:email)"),{"email":payload.email})
        if exists: raise HTTPException(409,"Email already exists.")
        uid=str(uuid4())
        await self.db.execute(text("INSERT INTO users(id,email,password_hash,name,phone,provider,is_active) VALUES(:id,:email,:password,:name,:phone,'admin_created',true)"),{"id":uid,"email":payload.email.strip().lower(),"password":hash_password(payload.temporaryPassword),"name":payload.name.strip(),"phone":payload.phone})
        for role in payload.roles: await self.db.execute(text("INSERT INTO user_role_assignments(user_id,role,assigned_by) VALUES(:id,:role,:actor)"),{"id":uid,"role":role,"actor":actor_id})
        await self.db.commit();return {"id":uid,"email":payload.email.strip().lower(),"name":payload.name.strip(),"roles":payload.roles,"isActive":True}
    async def replace_roles(self,user_id:str,roles:list[str],actor_id:str)->dict:
        if user_id==actor_id and "ADMIN" not in roles: raise HTTPException(422,"Cannot remove your own ADMIN role.")
        if "ADMIN" not in roles:
            count=await self.db.scalar(text("SELECT count(DISTINCT user_id) FROM user_role_assignments WHERE role='ADMIN'"))
            current=await self.db.scalar(text("SELECT 1 FROM user_role_assignments WHERE user_id=:id AND role='ADMIN'"),{"id":user_id})
            if current and count<=1: raise HTTPException(422,"Cannot remove the last ADMIN.")
        await self.db.execute(text("DELETE FROM user_role_assignments WHERE user_id=:id"),{"id":user_id})
        for role in roles: await self.db.execute(text("INSERT INTO user_role_assignments(user_id,role,assigned_by) VALUES(:id,:role,:actor)"),{"id":user_id,"role":role,"actor":actor_id})
        await self.db.commit();return {"id":user_id,"roles":roles}
    async def set_status(self,user_id:str,is_active:bool,actor_id:str)->dict:
        if not is_active and user_id==actor_id: raise HTTPException(422,"Cannot deactivate yourself.")
        result=await self.db.execute(text("UPDATE users SET is_active=:active,updated_at=now() WHERE id=:id"),{"id":user_id,"active":is_active})
        if not result.rowcount: raise HTTPException(404,"User not found.")
        await self.db.commit();return {"id":user_id,"isActive":is_active}
