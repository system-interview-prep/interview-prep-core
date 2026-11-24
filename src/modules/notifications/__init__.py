"""User notification delivery and read-state module."""

from src.core.module import AppModule
from src.modules.notifications.router import router


def build_module() -> AppModule:
    return AppModule(name="notifications", router=router)
