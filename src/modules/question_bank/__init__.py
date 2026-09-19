"""Controlled, versioned interview question bank."""

from src.core.module import AppModule
from src.modules.question_bank.router import router


def build_module() -> AppModule:
    return AppModule(name="question_bank", router=router)
