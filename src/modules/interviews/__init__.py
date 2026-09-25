"""Structured interview runtime foundation."""

from src.core.module import AppModule
from src.modules.interviews.router import router


def build_module() -> AppModule:
    return AppModule(name="interviews", router=router)
