"""Voice-to-agent input adapter.

Only final STT text is accepted as an interview turn.  Partial transcripts
should remain transport/UI data until explicitly finalized by the voice layer.
"""

from __future__ import annotations

from typing import Any

from src.modules.interviews.agent.contracts import InterviewAgentRequest


def from_voice_transcript(
    *,
    session_id: str,
    turn_index: int,
    final_transcript: str,
    duration_seconds: float = 0.0,
    telemetry: dict[str, Any] | None = None,
) -> InterviewAgentRequest:
    return InterviewAgentRequest(
        session_id=session_id,
        turn_index=turn_index,
        text_content=final_transcript,
        modality="VOICE",
        duration_seconds=duration_seconds,
        telemetry=telemetry or {},
    )
