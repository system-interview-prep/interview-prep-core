"""Admin feature package, root router, and backwards-compatible module exports."""

from importlib import import_module
import sys

from fastapi import APIRouter

from src.core.module import AppModule
from src.modules.admin.ai import router as ai_router
from src.modules.admin.overview import router as overview_router
from src.modules.admin.processing import router as processing_router
from src.modules.admin.processing.controller import (
    get_admin_cv_parsing_run,
    get_admin_matching_run,
    list_admin_cv_parsing_runs,
    list_admin_matching_runs,
)
from src.modules.admin.sessions import router as sessions_router
from src.modules.admin.sessions.controller import _access_log_filter, list_admin_sessions
from src.modules.admin.system import router as system_router

router = APIRouter(prefix="/admin", tags=["admin"])
router.include_router(overview_router)
router.include_router(sessions_router)
router.include_router(ai_router)
router.include_router(processing_router)
router.include_router(system_router)

_LEGACY_MODULES = {
    "ai_overview": "src.modules.admin.ai.ai_overview",
    "cv_parsing_runs": "src.modules.admin.processing.cv_parsing_runs",
    "langfuse_view": "src.modules.admin.ai.langfuse_view",
    "matching_runs": "src.modules.admin.processing.matching_runs",
    "practice_stats": "src.modules.admin.overview.practice_stats",
    "room_observer": "src.modules.admin.sessions.room_observer",
    "system_config": "src.modules.admin.system.system_config",
    "system_health": "src.modules.admin.system.system_health",
    "user_activity": "src.modules.admin.system.user_activity",
}
for _name, _path in _LEGACY_MODULES.items():
    _module = import_module(_path)
    globals()[_name] = _module
    sys.modules[f"{__name__}.{_name}"] = _module

# Keep historical import paths available without one-file shim modules at the root.
sys.modules[f"{__name__}.router"] = sys.modules[__name__]

del _name, _path, _module, _LEGACY_MODULES


def build_module() -> AppModule:
    return AppModule(name="admin", router=router)
