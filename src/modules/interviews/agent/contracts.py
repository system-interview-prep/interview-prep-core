"""Stable contracts at the boundary of the single Interview Agent.

These contracts deliberately contain transport-neutral data only.  WebSocket,
WebRTC, STT, TTS, and video providers belong in adapters and must not leak into
the agent's decision API.
"""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

InterviewModality = Literal["CHAT", "VOICE", "VIDEO"]


class InterviewAgentRequest(BaseModel):
    """One final candidate turn submitted to the Interview Agent."""

    session_id: UUID | str
    turn_index: int = Field(ge=0)
    text_content: str = Field(min_length=1)
    modality: InterviewModality = "CHAT"
    duration_seconds: float = Field(default=0.0, ge=0.0)
    telemetry: dict[str, Any] = Field(default_factory=dict)


class InterviewAgentResponse(BaseModel):
    """Decision returned by the agent and consumed by any modality adapter."""

    session_id: UUID | str
    turn_index: int
    action: str
    current_stage: str
    message_text: str
    is_session_finished: bool = False
    current_competency: str | None = None
    time_remaining_seconds: int = 0
    exit_reason: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
