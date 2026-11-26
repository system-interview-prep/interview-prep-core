"""Anticipation: speculative work while the candidate is still speaking.

INTERVIA_INTERVIEW_AGENT_V2_SPEC §7. Runs outside the Main LangGraph, reads
partial transcripts, and never blocks the critical path or commits anything.
"""

from src.modules.interviews.realtime.anticipation.engine import (
    AnticipationEngine,
    AnticipationStats,
)
from src.modules.interviews.realtime.anticipation.validator import (
    PreviewKey,
    PreviewRejected,
    preview_key,
    validate_preview,
)

__all__ = [
    "AnticipationEngine",
    "AnticipationStats",
    "PreviewKey",
    "PreviewRejected",
    "preview_key",
    "validate_preview",
]
