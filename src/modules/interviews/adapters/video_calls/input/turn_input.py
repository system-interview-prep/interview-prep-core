"""Video-to-agent input adapter.

Video observations are optional metadata.  They do not become hiring decisions
and do not replace the candidate's final answer transcript.
"""

from __future__ import annotations

from typing import Any

from src.modules.interviews.agent.contracts import InterviewAgentRequest


def from_video_turn(
    *,
    session_id: str,
    turn_index: int,
    final_transcript: str,
    duration_seconds: float = 0.0,
    video_observations: dict[str, Any] | None = None,
    telemetry: dict[str, Any] | None = None,
) -> InterviewAgentRequest:
    merged_telemetry = dict(telemetry or {})
    if video_observations:
        merged_telemetry["video_observations"] = video_observations
    return InterviewAgentRequest(
        session_id=session_id,
        turn_index=turn_index,
        text_content=final_transcript,
        modality="VIDEO",
        duration_seconds=duration_seconds,
        telemetry=merged_telemetry,
    )
