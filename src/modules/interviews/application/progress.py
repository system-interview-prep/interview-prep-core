"""Public compatibility facade for interview progress calculations and data loading.

Pure metrics live in :mod:`progress_skills` and :mod:`progress_sessions`; SQL
access lives in :mod:`progress_repository`. Existing imports from
``application.progress`` remain supported.
"""

from src.modules.interviews.application.progress_repository import (
    load_progress,
    set_weekly_goal,
    weekly_goal_for,
)
from src.modules.interviews.application.progress_sessions import *  # noqa: F403
from src.modules.interviews.application.progress_sessions import __all__ as _session_exports
from src.modules.interviews.application.progress_skills import *  # noqa: F403
from src.modules.interviews.application.progress_skills import __all__ as _skill_exports

__all__ = [*_skill_exports, *_session_exports, "load_progress", "set_weekly_goal", "weekly_goal_for"]
