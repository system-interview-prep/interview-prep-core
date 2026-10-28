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

# Environments allowed to receive the Question Bank dev fixtures.
_FIXTURE_ENVIRONMENTS = frozenset({"development", "dev", "test", "testing", "local"})


def _fixtures_allowed(settings: Settings | None) -> bool:
    """Decide whether dev/demo fixtures may be written.

    Fail closed when settings are unavailable: an unknown environment must never
    be assumed to be development. The previous version read a non-existent
    ``settings.APP_ENV`` attribute and accepted an empty environment string, so
    every caller that omitted settings - including the application lifespan -
    silently seeded demo questions into whatever database it was pointed at.
    """
    if settings is None:
        return False
    explicit = getattr(settings, "question_bank_seed_enabled", None)
    if explicit is not None:
        return bool(explicit)
    return (settings.app_env or "").strip().lower() in _FIXTURE_ENVIRONMENTS


async def run_all_seeds(
    session_factory: Callable[[], AsyncSession], settings: Settings | None = None
) -> bool:
    """Run all idempotent application seeds in dependency order."""
    created_admin = await seed_admin(session_factory, settings)
    await seed_taxonomy(session_factory)

    # Question Bank dev fixtures: only in non-production environments.
    if _fixtures_allowed(settings):
        summary = await seed_question_bank(session_factory)
        newly_seeded = sum(summary.values())
        if newly_seeded:
            logger.info("Question Bank seed created %d new question(s): %s", newly_seeded, summary)
        else:
            logger.debug("Question Bank seed: all fixtures already present.")
    else:
        logger.warning(
            "Question Bank seed skipped for app_env=%r. Interviews fail closed until the bank "
            "has approved questions; set QUESTION_BANK_SEED_ENABLED=true to seed it.",
            settings.app_env if settings else None,
        )

    return created_admin
