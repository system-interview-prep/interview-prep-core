"""Central application data seed entrypoint."""

import logging
from collections.abc import Callable

from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import Settings
from src.seeds.admin_seed import seed_admin
from src.seeds.question_bank_seed import seed_question_bank
from src.seeds.taxonomy_seed import seed_taxonomy

logger = logging.getLogger(__name__)

__all__ = ["run_all_seeds", "seed_admin", "seed_taxonomy", "seed_question_bank"]


async def run_all_seeds(
    session_factory: Callable[[], AsyncSession], settings: Settings | None = None
) -> bool:
    """Run all idempotent application seeds in dependency order."""
    created_admin = await seed_admin(session_factory, settings)
    await seed_taxonomy(session_factory)

    # Question Bank dev fixtures: only in non-production environments.
    env = (settings.APP_ENV if settings else "") or ""
    if env.lower() in {"development", "dev", "test", "testing", ""}:
        summary = await seed_question_bank(session_factory)
        newly_seeded = sum(summary.values())
        if newly_seeded:
            logger.info("Question Bank seed created %d new question(s): %s", newly_seeded, summary)
        else:
            logger.debug("Question Bank seed: all fixtures already present.")

    return created_admin
