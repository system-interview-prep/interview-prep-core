"""Structured interview runtime and its Chat/Voice/Video adapters."""

from fastapi import APIRouter

from src.core.module import AppModule
from src.modules.interviews.adapters.chat import router as chat_adapter_router
from src.modules.interviews.adapters.video_calls import router as video_calls_adapter_router
from src.modules.interviews.adapters.voice import router as voice_adapter_router
from src.modules.interviews.agent import InterviewAgent
from src.modules.interviews.api.router import router as interview_router
from src.modules.interviews.api.session_router import router as session_router

router = APIRouter()
router.include_router(interview_router)
router.include_router(session_router)
router.include_router(chat_adapter_router)
router.include_router(voice_adapter_router)
router.include_router(video_calls_adapter_router)


def build_module() -> AppModule:
    return AppModule(name="interviews", router=router)


__all__ = ["InterviewAgent", "build_module", "router"]
