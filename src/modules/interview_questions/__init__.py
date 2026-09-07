"""Interview agenda, question identity and progression module."""

from src.core.module import AppModule
from src.modules.interview_questions.router import router


def build_module() -> AppModule:
    return AppModule(name="interview_questions", router=router)
