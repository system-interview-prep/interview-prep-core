"""The usable interview engine: Core's rules plus the Interviewer's inference.

INTERVIA_INTERVIEW_AGENT_V2_SPEC §10 and the V2 correction brief §2. Core owns
the turn's rules and knows nothing about models; ``response_generation`` owns
the model calls and knows nothing about the stage machine. This is the one
place the two are put together, so Core's no-provider boundary survives while
every existing caller keeps the engine it has always built.

Construction is unchanged::

    engine = InterviewCoreEngine(llm_client=..., ai_generator=..., candidate_tone=...)

The mixin comes first in the MRO so its model-facing methods win over the
placeholders ``CoreTurnEngine`` falls back to when nobody supplied one.
"""

from __future__ import annotations

from src.modules.interviews.agents.interviewer.response_generation import InterviewResponseMixin
from src.modules.interviews.core.conversation.turn_engine import CoreTurnEngine

__all__ = ["InterviewCoreEngine"]


class InterviewCoreEngine(InterviewResponseMixin, CoreTurnEngine):
    """Headless Interview Engine shared 100% by the Chat, Voice and Video paths."""
