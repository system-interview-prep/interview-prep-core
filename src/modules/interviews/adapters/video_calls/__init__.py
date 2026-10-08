"""Video-call transport and lifecycle adapter for the Interview Agent."""

from src.modules.interviews.adapters.video_calls import signaling as _signaling
from src.modules.interviews.adapters.video_calls.router import router

del _signaling

__all__ = ["router"]
