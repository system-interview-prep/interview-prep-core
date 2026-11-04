"""Public matching operations for other feature modules.

The implementation lives in ``matching.application.facade``; this is the only
entry point sibling modules are allowed to import (see ``matching.schemas`` for
why).
"""

from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.matching.application.facade import (
    canonical_job_from_description,
    evaluate_match,
)
from src.modules.matching.application.fit_history import get_fit_attempt as _get_fit_attempt


async def get_candidate_fit_attempt(
    db: AsyncSession, *, user_id: str, attempt_id: str
) -> dict[str, object] | None:
    """Read one CV–JD assessment scoped to its owning candidate."""
    return await _get_fit_attempt(db, user_id=user_id, attempt_id=attempt_id)

__all__ = [
    "canonical_job_from_description",
    "evaluate_match",
    "get_candidate_fit_attempt",
]
