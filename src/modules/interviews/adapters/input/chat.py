"""Chat-to-agent input adapter."""

from __future__ import annotations

from typing import Any

from src.modules.interviews.agent.contracts import InterviewAgentRequest


def from_chat_message(
    *,
    session_id: str,
    turn_index: int,
    content: str,
    telemetry: dict[str, Any] | None = None,
) -> InterviewAgentRequest:
    return InterviewAgentRequest(
        session_id=session_id,
        turn_index=turn_index,
        text_content=content,
        modality="CHAT",
        telemetry=telemetry or {},
    )
