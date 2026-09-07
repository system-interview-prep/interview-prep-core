"""Speech-to-text and text-to-speech provider adapters module."""

from src.core.module import AppModule
from src.modules.voice.router import router


def build_module() -> AppModule:
    return AppModule(name="voice", router=router)
