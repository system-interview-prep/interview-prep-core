"""Public runtime operations consumed by other application modules."""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.interviews.application.agent_runtime import database_operations, run_interview_command
from src.modules.interviews.application.chat_runtime import ChatRuntimeError


async def start_voice_interview(db: AsyncSession, session_row: dict[str, Any]) -> dict[str, Any]:
    """Start or resume a voice interview using the canonical chat runtime."""
    return await run_interview_command(
        session_id=str(session_row["id"]), command="open",
        operations=database_operations(db=db, user={}, session_row=session_row),
    )


async def submit_voice_answer(
    db: AsyncSession,
    session_row: dict[str, Any],
    *,
    client_message_id: str,
    content: str,
    telemetry: dict[str, Any] | None = None,
    duration_seconds: float = 0.0,
    modality: str = "VOICE",
) -> dict[str, Any]:
    """Submit a final media transcript to the modality-neutral Interview Core."""
    return await run_interview_command(
        session_id=str(session_row["id"]), command="respond", event_id=client_message_id,
        operations=database_operations(db=db, user={}, session_row=session_row),
        payload={"client_message_id": client_message_id, "content": content,
                 "telemetry": telemetry or {}, "modality": modality,
                 "duration_seconds": duration_seconds},
    )


__all__ = ["ChatRuntimeError", "start_voice_interview", "submit_voice_answer"]
