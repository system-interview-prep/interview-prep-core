"""Candidate answer and interview report scoring module."""

from src.core.module import AppModule
from src.modules.scoring.router import router


def build_module() -> AppModule:
    return AppModule(name="scoring", router=router)
