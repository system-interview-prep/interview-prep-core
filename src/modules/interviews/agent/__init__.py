"""Command runtime of the interview: one durable, idempotent entry point.

``InterviewAgent.execute`` runs one of six session commands (prepare, freeze,
open, respond, finish, score) on a checkpointed LangGraph thread. The decision
inside a candidate turn is the core engine's (``core.conversation``); the turn
contract is ``core.contracts.interview`` for every modality. Decision-making by
the model arrives with INTERVIEW_AGENT_DECISION_PLAN_V1 (Pha 1+).
"""

from src.modules.interviews.agent.interview_agent import InterviewAgent

__all__ = ["InterviewAgent"]
