"""Interview chat history and conversational orchestration module."""

from src.core.module import AppModule
from src.modules.chat.router import router


def build_module() -> AppModule:
    return AppModule(name="chat", router=router)
