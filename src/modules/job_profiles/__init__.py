"""Job description ownership, upload and parse lifecycle module."""

from src.core.module import AppModule
from src.modules.job_profiles.router import router


def build_module() -> AppModule:
    return AppModule(name="job_profiles", router=router)
