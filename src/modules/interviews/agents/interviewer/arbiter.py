"""The Response Arbiter: deterministic, and never a third model call.

INTERVIA_INTERVIEW_AGENT_V2_SPEC §6.4 and the V2 correction brief §7. The
Thinker is the authority; the Talker is a guess made in parallel. The Arbiter
decides whether the guess may be *spoken* — not whether it may *decide*, which
it never may.

It reuses the Talker's predicted line only when it can prove equivalence by
inspection: same action, same target, the action is still in the menu, the
Thinker did not already supply its own line, and the predicted line passes the
same output guard the Thinker's would. "Same action" alone is not enough
(§6.4); a probe aimed at a different missing point is a different question
wearing the same label. When equivalence cannot be shown, the Thinker's
response stands and the Talker's is discarded — and that is counted, because
the Luna match rate of §13 is this ratio.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Optional

from src.modules.interviews.agents.interviewer.talker import TalkerOutput
from src.modules.interviews.agents.interviewer.tools import UNIFIED_TEXT_ACTIONS
from src.modules.interviews.core.conversation.turn_context import Decision, TurnContext
from src.modules.interviews.core.guardrails.output import (
    checked_probe,
    clarify_rejection_reason,
    redirect_rejection_reason,
)

__all__ = ["ArbiterResult", "arbitrate"]


@dataclass(frozen=True)
class ArbiterResult:
    """The decision that will execute, and what became of the prediction."""

    decision: Decision
    talker_reused: bool
    talker_outcome: str  # reused | no_talker | action_mismatch | target_mismatch | thinker_has_text | guard:<reason> | no_response | outside_menu
    acknowledgment: Optional[str]


def _targets_match(thinker: Decision, talker: TalkerOutput) -> bool:
    """Equal targets, or the Thinker names none and neither does the Talker."""
    mine = str(thinker.args.get("missing_aspect") or "").strip().casefold()
    theirs = str(talker.predicted_target or "").strip().casefold()
    return mine == theirs


def _guard_reason(ctx: TurnContext, action: str, text: str) -> Optional[str]:
    """The action's own output guard, exactly as the Thinker's text would face."""
    question = ctx.active_prompt or ctx.question_prompt
    rubric = str(ctx.current_question.get("rubric_criteria") or "")
    argument = UNIFIED_TEXT_ACTIONS.get(action)
    if argument == "probe_text":
        return None if checked_probe(text, question=question, answer=ctx.candidate_text, rubric=rubric) else "probe"
    if argument == "clarify_text":
        return clarify_rejection_reason(text, question=question, answer=ctx.candidate_text, rubric=rubric)
    if argument == "redirect_text":
        return redirect_rejection_reason(text, answer=ctx.candidate_text, question=question, rubric=rubric)
    return "no_speaking_argument"


def arbitrate(
    ctx: TurnContext,
    *,
    thinker: Decision,
    talker: Optional[TalkerOutput],
    allowed_actions: List[str],
) -> ArbiterResult:
    """Return the Thinker's decision, with the Talker's line attached only if proven safe."""
    ack = talker.acknowledgment if talker is not None else None

    def keep(outcome: str) -> ArbiterResult:
        return ArbiterResult(decision=thinker, talker_reused=False, talker_outcome=outcome, acknowledgment=ack)

    if talker is None:
        return keep("no_talker")
    if thinker.action not in allowed_actions:
        return keep("outside_menu")
    argument = UNIFIED_TEXT_ACTIONS.get(thinker.action)
    if argument is None:
        return keep("no_speaking_argument")
    if thinker.args.get(argument):
        # The Thinker already wrote the official line; it wins (§6.4).
        return keep("thinker_has_text")
    if talker.predicted_action != thinker.action:
        return keep("action_mismatch")
    if not _targets_match(thinker, talker):
        return keep("target_mismatch")
    text = str(talker.predicted_response or "").strip()
    if not text:
        return keep("no_response")
    reason = _guard_reason(ctx, thinker.action, text)
    if reason:
        return keep(f"guard:{reason}")

    merged = Decision(
        thinker.action,
        {**thinker.args, argument: text, "talker_reused": True},
        reason=thinker.reason,
        evidence_refs=list(thinker.evidence_refs),
        source=thinker.source,
        path=[*thinker.path, "talker_reused"],
    )
    return ArbiterResult(decision=merged, talker_reused=True, talker_outcome="reused", acknowledgment=ack)
