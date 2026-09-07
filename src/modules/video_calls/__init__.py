"""Video-call business lifecycle module."""
from src.core.module import AppModule
from src.modules.video_calls.router import router


def build_module() -> AppModule:
    return AppModule(name="video_calls", router=router)
