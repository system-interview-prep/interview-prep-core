"""Single-agent boundary for the modality-agnostic interview runtime.

The agent package is intentionally small at first.  Existing planner, question
selector, and core engine modules remain backwards compatible while callers get
one stable entry point for Chat, Voice, and Video.
"""

from src.modules.interviews.agent.contracts import (
    InterviewAgentRequest,
    InterviewAgentResponse,
    InterviewModality,
)
from src.modules.interviews.agent.interview_agent import InterviewAgent

__all__ = [
    "InterviewAgent",
    "InterviewAgentRequest",
    "InterviewAgentResponse",
    "InterviewModality",
]
