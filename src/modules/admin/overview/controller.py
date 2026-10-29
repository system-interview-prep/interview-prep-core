from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.security import require_admin
from src.infrastructure.database import get_db
from src.modules.admin.overview.service import get_overview, get_practice

router = APIRouter(tags=["admin"])


@router.get("/overview")
async def get_admin_overview(
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await get_overview(db)


@router.get("/practice")
async def get_admin_practice(
    weeks: int = Query(8, ge=2, le=26),
    include_demo: bool = Query(False, alias="includeDemo"),
    _: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await get_practice(db, weeks=weeks, include_demo=include_demo)
