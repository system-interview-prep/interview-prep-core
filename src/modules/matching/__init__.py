from fastapi import APIRouter

from src.core.module import AppModule


def build_module() -> AppModule:
    """Load transport dependencies only while composing the HTTP application."""
    from src.modules.matching.clarification_router import router as clarification_router
    from src.modules.matching.router import router as matching_router

    router = APIRouter()
    router.include_router(matching_router)
    router.include_router(clarification_router)
    return AppModule(name="matching", router=router)
