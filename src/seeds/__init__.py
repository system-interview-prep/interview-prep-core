"""Central application data seed entrypoint."""

from collections.abc import Callable

from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import Settings
from src.seeds.admin_seed import seed_admin
from src.seeds.taxonomy_seed import seed_taxonomy


async def run_all_seeds(
    session_factory: Callable[[], AsyncSession], settings: Settings | None = None
) -> bool:
    """Run all idempotent application seeds in dependency order."""
    created_admin = await seed_admin(session_factory, settings)
    await seed_taxonomy(session_factory)
    return created_admin
