"""Interview session lifecycle module."""

from src.core.module import AppModule
from src.modules.sessions.router import router


def build_module() -> AppModule:
    return AppModule(name="sessions", router=router)
