from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.security import current_user
from src.infrastructure.database import get_db

router = APIRouter(prefix="/users", tags=["users"])


class ProfilePatch(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    dob: date | None = None


def _profile(row: dict) -> dict:
    return {
        "id": row["id"],
        "email": row["email"],
        "name": row["name"],
        "role": row["role"],
        "provider": row["provider"],
        "dob": row["dob"].isoformat() if row["dob"] else None,
        "picture": row["picture"],
        "createdAt": row["created_at"].isoformat(),
    }


async def _load_profile(db: AsyncSession, user_id: str) -> dict:
    result = await db.execute(
        text("SELECT id, email, name, role, provider, dob, picture, created_at FROM users WHERE id = :id"),
        {"id": user_id},
    )
    row = result.mappings().one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="User not found")
    return _profile(row)


@router.get("/me")
async def get_profile(user: dict = Depends(current_user), db: AsyncSession = Depends(get_db)) -> dict:
    return await _load_profile(db, user["sub"])


@router.patch("/me")
async def update_profile(
    payload: ProfilePatch, user: dict = Depends(current_user), db: AsyncSession = Depends(get_db)
) -> dict:
    values = payload.model_dump(exclude_unset=True)
    if values:
        await db.execute(
            text(
                "UPDATE users SET name = COALESCE(:name, name), "
                "dob = COALESCE(CAST(:dob AS date), dob), updated_at = now() "
                "WHERE id = :id"
            ),
            {"id": user["sub"], "name": values.get("name"), "dob": values.get("dob")},
        )
        await db.commit()
    return await _load_profile(db, user["sub"])
