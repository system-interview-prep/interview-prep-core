"""CV ownership, upload, storage metadata and parse lifecycle module."""

from src.core.module import AppModule
from src.modules.user_cvs.router import router


def build_module() -> AppModule:
    return AppModule(name="user_cvs", router=router)
