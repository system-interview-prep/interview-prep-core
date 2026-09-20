from base64 import urlsafe_b64decode, urlsafe_b64encode
from binascii import Error as BinasciiError
from uuid import uuid4
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from src.core.roles import USER_ROLES
from src.core.security import hash_password
from src.modules.admin_users.schemas import CreateAdminUserRequest

class AdminUserService:
    def __init__(self, db: AsyncSession): self.db=db
    @staticmethod
    def _decode_cursor(cursor: str | None) -> int:
        if not cursor:
            return 0
        try:
            return max(0, int(urlsafe_b64decode(cursor.encode()).decode()))
        except (BinasciiError, ValueError, UnicodeDecodeError):
            raise HTTPException(422, "Invalid cursor.") from None

    @staticmethod
    def _encode_cursor(offset: int) -> str:
        return urlsafe_b64encode(str(offset).encode()).decode()

    @staticmethod
    def _filters(query: str | None, role: str | None, active: bool | None) -> tuple[str, dict]:
        normalized_role = role.strip().upper() if role else None
        if normalized_role and normalized_role not in USER_ROLES:
            raise HTTPException(422, "Invalid role filter.")
        return """WHERE (CAST(:q AS text) IS NULL OR lower(u.email) LIKE lower(CAST(:q AS text))
                       OR lower(u.name) LIKE lower(CAST(:q AS text)))
          AND (CAST(:active AS boolean) IS NULL OR u.is_active = CAST(:active AS boolean))
          AND (CAST(:role AS text) IS NULL OR EXISTS (SELECT 1 FROM user_role_assignments filter_role
                   WHERE filter_role.user_id = u.id AND filter_role.role = CAST(:role AS text)))""", {
            "q": f"%{query.strip()}%" if query and query.strip() else None,
            "role": normalized_role,
            "active": active,
        }

    async def list_users(self, query: str | None, role: str | None, active: bool | None, cursor: str | None, limit: int) -> dict:
        where, params = self._filters(query, role, active)
        offset = self._decode_cursor(cursor)
        total = await self.db.scalar(text(f"SELECT count(*) FROM users u {where}"), params) or 0
        rows = await self.db.execute(text(f"""SELECT u.id, u.email, u.name, u.provider, u.is_active, u.created_at,
            array_agg(ura.role ORDER BY ura.role) AS roles
            FROM users u JOIN user_role_assignments ura ON ura.user_id = u.id {where}
            GROUP BY u.id ORDER BY u.created_at DESC, u.id DESC LIMIT :limit OFFSET :offset"""), {**params, "limit": limit + 1, "offset": offset})
        items = [dict(row) for row in rows.mappings().all()]
        has_next = len(items) > limit
        return {"items": items[:limit], "total": total, "nextCursor": self._encode_cursor(offset + limit) if has_next else None}

    async def get_user(self, user_id: str) -> dict:
        rows = await self.db.execute(text("""SELECT u.id, u.email, u.name, u.provider, u.is_active, u.created_at,
            array_agg(ura.role ORDER BY ura.role) AS roles FROM users u
            JOIN user_role_assignments ura ON ura.user_id = u.id WHERE u.id = :id GROUP BY u.id"""), {"id": user_id})
        row = rows.mappings().one_or_none()
        if row is None: raise HTTPException(404, "User not found.")
        return dict(row)
    async def create(self, payload: CreateAdminUserRequest, actor_id: str) -> dict:
        exists=await self.db.scalar(text("SELECT 1 FROM users WHERE lower(email)=lower(:email)"),{"email":payload.email})
        if exists: raise HTTPException(409,"Email already exists.")
        uid=str(uuid4())
        try:
            await self.db.execute(text("INSERT INTO users(id,email,password_hash,name,phone,provider,is_active) VALUES(:id,:email,:password,:name,:phone,'admin_created',true)"),{"id":uid,"email":payload.email,"password":hash_password(payload.temporaryPassword),"name":payload.name,"phone":payload.phone})
            for role in payload.roles: await self.db.execute(text("INSERT INTO user_role_assignments(user_id,role,assigned_by) VALUES(:id,:role,:actor)"),{"id":uid,"role":role,"actor":actor_id})
            await self.db.execute(text("INSERT INTO user_credits(id,user_id,cv_scans_remaining,voice_mock_remaining,plan_tier) VALUES(:id,:user_id,3,1,'FREE')"), {"id": str(uuid4()), "user_id": uid})
            await self.db.commit()
        except Exception:
            await self.db.rollback()
            raise
        return {"id":uid,"email":payload.email,"name":payload.name,"roles":payload.roles,"isActive":True}
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
