"""Job description ownership, upload and parse lifecycle module."""

from src.core.module import AppModule
from src.modules.job_descriptions.router import router


def build_module() -> AppModule:
    return AppModule(name="job_descriptions", router=router)
