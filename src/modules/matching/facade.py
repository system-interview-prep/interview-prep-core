"""Public matching operations for other feature modules.

The implementation lives in ``matching.application.facade``; this is the only
entry point sibling modules are allowed to import (see ``matching.schemas`` for
why).
"""

from src.modules.matching.application.facade import (
    canonical_job_from_description,
    evaluate_match,
)

__all__ = [
    "canonical_job_from_description",
    "evaluate_match",
]
