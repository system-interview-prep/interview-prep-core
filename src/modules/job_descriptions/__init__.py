"""Job description ownership, upload and parse lifecycle module."""

from src.core.module import AppModule
def build_module() -> AppModule:
    from src.modules.job_descriptions.router import router

    return AppModule(name="job_descriptions", router=router)
