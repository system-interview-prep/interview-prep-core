"""Default taxonomy seed facade."""

from collections.abc import Callable

from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.taxonomy.seed import seed_default_taxonomy


async def seed_taxonomy(session_factory: Callable[[], AsyncSession]) -> bool:
    return await seed_default_taxonomy(session_factory)
