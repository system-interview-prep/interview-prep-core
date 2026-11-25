"""Main graph: command routing, execution, checkpoint and idempotency.

INTERVIA_INTERVIEW_AGENT_V2_SPEC §4 and §10. One compiled LangGraph dispatches
the six session commands (``prepare``, ``freeze``, ``open``, ``respond``,
``finish``, ``score``); it is orchestration infrastructure, not an LLM agent.
SQL stays the source of truth for the business, and the checkpoint only helps
execution recover.

``runtime.py`` is the entry point callers use; ``command_runner.py`` owns the
durable per-``(session, command, event)`` thread; ``checkpoints.py`` owns the
Postgres checkpointer and the candidate-memory store lifecycle.
"""

from src.modules.interviews.graph.command_runner import InterviewCommandRunner

__all__ = ["InterviewCommandRunner"]
