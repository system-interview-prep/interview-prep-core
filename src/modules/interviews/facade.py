"""Public runtime operations consumed by other application modules."""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.interviews.chat_runtime import (
    ChatRuntimeError,
    process_candidate_message,
    start_chat_session,
)


async def start_voice_interview(db: AsyncSession, session_row: dict[str, Any]) -> dict[str, Any]:
    """Start or resume a voice interview using the canonical chat runtime."""
    return await start_chat_session(db=db, session_row=session_row)


async def submit_voice_answer(
    db: AsyncSession,
    session_row: dict[str, Any],
    *,
    client_message_id: str,
    content: str,
    telemetry: dict[str, Any] | None = None,
    duration_seconds: float = 0.0,
) -> dict[str, Any]:
    """Submit an STT transcript to Interview Core as a voice turn."""
    return await process_candidate_message(
        db=db,
        session_row=session_row,
        client_message_id=client_message_id,
        content=content,
        telemetry=telemetry,
        modality="VOICE",
        duration_seconds=duration_seconds,
    )


__all__ = ["ChatRuntimeError", "start_voice_interview", "submit_voice_answer"]
