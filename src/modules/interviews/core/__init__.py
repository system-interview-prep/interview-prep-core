"""Modality-agnostic interview domain: contracts, rules and guardrails.

The composed engine is **not** exported here. Core declares the turn's rules
and the :class:`TurnInference` port; ``agents.interviewer.engine`` is where the
rules and the model-facing half are put together. Re-exporting it would point
Core at the agents and undo the V2 brief §2 boundary.
"""
from src.modules.interviews.core.contracts.interview import (
    CandidateTurnInput,
    InterviewerTurnOutput,
    InterviewStage,
    QuestionItem,
    SessionExitReason,
    TurnAction,
)

__all__ = [
    "CandidateTurnInput",
    "InterviewerTurnOutput",
    "InterviewStage",
    "QuestionItem",
    "SessionExitReason",
    "TurnAction",
]
