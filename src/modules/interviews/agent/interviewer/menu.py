"""The menu: which actions a decider may choose on this turn, and why not the others.

Code decides the menu; the decider (rule ladder today, model later) chooses
from it. An action absent from the menu cannot be chosen — that is how the
probe budget, the clock and the blueprint stay rules while the choice moves
to the model (INTERVIEW_AGENT_DECISION_PLAN_V1 §4.3).

The gates reuse ``core.conversation.turn_context.probe_window`` so the menu and
the rule ladder can never disagree about whether a probe is allowed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List

from src.modules.interviews.core.contracts.interview import InterviewStage
from src.modules.interviews.core.conversation.turn_context import ACTIONS, TurnContext, probe_window

# Actions a candidate's own words can always trigger (control of the conversation).
CONVERSATIONAL = ("repeat_question", "answer_candidate_question", "decline_system_probe", "redirect_off_topic")


@dataclass
class Menu:
    allowed: List[str]
    reasons: Dict[str, str] = field(default_factory=dict)  # action → why it is not on the menu

    def as_record(self) -> Dict[str, object]:
        return {"allowed_actions": list(self.allowed), "menu_reasons": dict(self.reasons)}


def build_menu(ctx: TurnContext, *, memory_enabled: bool = False) -> Menu:
    """Filter ``ACTIONS`` by the state of the turn. Never returns an empty menu."""
    state = ctx.session_state
    allowed: List[str] = []
    reasons: Dict[str, str] = {}
    stage = ctx.current_stage

    def allow(action: str) -> None:
        allowed.append(action)

    def deny(action: str, why: str) -> None:
        reasons[action] = why

    # Ending the interview is the candidate's or the clock's to start; the decider may
    # only close when nothing is left to ask. The rule ladder's other closes (timeout,
    # skip on the last question, repeated give-ups) come from guards and counters.
    allow("confirm_abort")
    for action in CONVERSATIONAL:
        allow(action)

    if stage == InterviewStage.CLOSING:
        allow("closing_qna")
        for action in ("clarify_question", "continue_warm_up", "acknowledge_and_probe", "move_to_next_question"):
            deny(action, "closing stage: reverse Q&A only")
        deny("close_interview", "closing handler ends the session itself")
        return Menu(allowed, reasons)
    deny("closing_qna", "not in closing stage")

    if stage == InterviewStage.WARM_UP:
        allow("continue_warm_up")
        deny("acknowledge_and_probe", "warm-up never probes")
        deny("clarify_question", "warm-up uses continue_warm_up")
    else:
        deny("continue_warm_up", "only in warm-up")
        if ctx.clarify_count >= 1:
            deny("clarify_question", "one clarification per question")
        else:
            allow("clarify_question")
        window = probe_window(ctx)
        if window.open:
            allow("acknowledge_and_probe")
        else:
            closed = [name for name, hit in window.reasons.items() if hit]
            deny("acknowledge_and_probe", ",".join(closed) or "probe window closed")

    allow("move_to_next_question")

    queue_empty = _no_question_left(state)
    if queue_empty:
        allow("close_interview")
    else:
        deny("close_interview", "questions remain")

    if memory_enabled:
        allow("search_candidate_memory")
    else:
        deny("search_candidate_memory", "candidate memory off")

    assert allowed, "a turn menu can never be empty"
    for action in ACTIONS:
        assert action in allowed or action in reasons, f"menu forgot {action}"
    return Menu(allowed, reasons)


def _no_question_left(state: dict) -> bool:
    asked = {str(q) for q in state.get("asked_question_ids", []) or []}
    pool = state.get("questions_pool", {}) or {}
    for stage in (InterviewStage.VALIDATE, InterviewStage.DEEP_DIVE, InterviewStage.CHALLENGE, InterviewStage.BEHAVIORAL):
        for q in pool.get(stage.value, []) or []:
            if str(q.get("question_id") or q.get("question_version_id") or "") not in asked:
                return False
    return True
