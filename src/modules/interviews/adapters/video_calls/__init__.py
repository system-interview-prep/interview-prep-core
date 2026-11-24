"""Video-call transport and lifecycle adapter for the Interview Agent."""

from src.modules.interviews.adapters.video_calls.api import signaling as _signaling
from src.modules.interviews.adapters.video_calls.api.router import router

__all__ = ["router"]
