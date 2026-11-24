"""Interview agent packages and session workflow compatibility exports.

``InterviewCommandRunner`` executes durable session commands. It is workflow
infrastructure, not the Planner Agent or the per-turn Interviewer Agent.
"""

from src.modules.interviews.agent.session_workflow.command_runner import InterviewCommandRunner

# Backwards-compatible public name used by the module facade and older callers.
InterviewAgent = InterviewCommandRunner

__all__ = ["InterviewAgent", "InterviewCommandRunner"]
