"""Bootstrap administrator seed."""

from collections.abc import Callable

from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import Settings
from src.core.data_initializer import initialize_data


async def seed_admin(
    session_factory: Callable[[], AsyncSession], settings: Settings | None = None
) -> bool:
    return await initialize_data(session_factory, settings)
