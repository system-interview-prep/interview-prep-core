from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.security import current_user
from src.infrastructure.database import get_db

router = APIRouter(prefix="/admin/job-categories", tags=["job-categories"])


class CategoryInput(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str = ""


class CategoryPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None


def _category(row: dict) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "description": row["description"],
        "createdAt": row["created_at"].isoformat(),
        "updatedAt": row["updated_at"].isoformat(),
    }


async def _get(db: AsyncSession, category_id: str) -> dict:
    result = await db.execute(
        text("SELECT id, name, description, created_at, updated_at FROM job_categories WHERE id = :id"),
        {"id": category_id},
    )
    row = result.mappings().one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Job category not found")
    return _category(row)


@router.post("", status_code=status.HTTP_201_CREATED)
async def create(
    payload: CategoryInput, _: dict = Depends(current_user), db: AsyncSession = Depends(get_db)
) -> dict:
    category_id = str(uuid4())
    try:
        await db.execute(
            text("INSERT INTO job_categories (id, name, description) VALUES (:id, :name, :description)"),
            {"id": category_id, "name": payload.name.strip(), "description": payload.description},
        )
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail="Job category already exists") from exc
    return await _get(db, category_id)


@router.get("")
async def list_categories(
    _: dict = Depends(current_user),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=200),
    q: str | None = None,
    cursor: str | None = None,
) -> dict:
    del cursor
    query = "SELECT id, name, description, created_at, updated_at FROM job_categories"
    params: dict[str, object] = {"limit": limit}
    if q and q.strip():
        query += " WHERE lower(name) LIKE lower(:query)"
        params["query"] = f"%{q.strip()}%"
    query += " ORDER BY created_at DESC LIMIT :limit"
    result = await db.execute(text(query), params)
    return {"items": [_category(row) for row in result.mappings().all()]}


@router.get("/{category_id}")
async def get_category(
    category_id: str, _: dict = Depends(current_user), db: AsyncSession = Depends(get_db)
) -> dict:
    return await _get(db, category_id)


@router.patch("/{category_id}")
async def update(
    category_id: str,
    payload: CategoryPatch,
    _: dict = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    values = payload.model_dump(exclude_unset=True)
    if values:
        await db.execute(
        text(
            "UPDATE job_categories SET name = COALESCE(:name, name), "
            "description = COALESCE(:description, description), updated_at = now() "
            "WHERE id = :id"
        ),
            {"id": category_id, "name": values.get("name"), "description": values.get("description")},
        )
        await db.commit()
    return await _get(db, category_id)


@router.delete("/{category_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove(
    category_id: str, _: dict = Depends(current_user), db: AsyncSession = Depends(get_db)
) -> None:
    result = await db.execute(text("DELETE FROM job_categories WHERE id = :id"), {"id": category_id})
    await db.commit()
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Job category not found")
