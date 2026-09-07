from src.core.module import AppModule


def build_module() -> AppModule:
    """Load transport dependencies only while composing the HTTP application."""
    from src.modules.matching.router import router

    return AppModule(name="matching", router=router)
