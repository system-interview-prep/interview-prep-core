"""Candidate answer and interview report scoring module."""

from src.core.module import AppModule


def build_module() -> AppModule:
    from src.modules.scoring.router import router

    return AppModule(name="scoring", router=router)
