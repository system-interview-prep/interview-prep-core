"""What one candidate turn knows before anyone decides anything.

``TurnContext`` is built once per turn from the input and the session state
(timing, stage, the question on the table, counters, blueprint). The rule
ladder, the menu of allowed actions, the pacing block and every ``act_*`` read
it; only ``guards_in`` and the rule ladder write the few fields marked so.

``Decision`` is what a decider returns — today the rule ladder, later the
model — and what ``act`` executes. ``evidence_refs`` point at the material the
decision rests on (``answer:sent:n``, ``expected_point:k``, ``rule:#7``), the
same way P4 grading points at ``evidence_quotes``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID

from src.modules.interviews.core.contracts.interview import CandidateTurnInput, InterviewStage
from src.modules.interviews.core.session_policy.blueprint import InterviewBlueprint, blueprint_from_payload
from src.modules.interviews.core.session_policy.demo_mode import DEMO_OVERTIME_GRACE_SECONDS
from src.modules.interviews.core.conversation.turn_rules import pacing_seconds

# One follow-up can identify which experience the candidate means; a second
# can clarify their concrete role or evidence before the engine changes topic.
MAX_PROBES_PER_QUESTION = 2

# The closed set of actions a decider may choose. Each has one ``act_*`` in
# ``turn_engine`` and one tool schema in ``agent/tools.py`` (Pha 2).
ACTIONS: tuple[str, ...] = (
    "confirm_abort",
    "close_interview",
    "move_to_next_question",
    "repeat_question",
    "answer_candidate_question",
    "decline_system_probe",
    "redirect_off_topic",
    "clarify_question",
    "continue_warm_up",
    "acknowledge_and_probe",
    "closing_qna",
)


@dataclass
class Decision:
    """One chosen action with its reasons; ``args`` are the action's parameters."""

    action: str
    args: Dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    evidence_refs: List[str] = field(default_factory=list)
    # Which decider produced it and the path it took (rule rungs, or the model's call id).
    source: str = "rule"
    path: List[str] = field(default_factory=list)

    def as_record(self) -> Dict[str, Any]:
        return {
            "action": self.action,
            "args": dict(self.args),
            "reason": self.reason,
            "evidence_refs": list(self.evidence_refs),
            "source": self.source,
            "path": list(self.path),
        }


@dataclass
class TurnContext:
    turn_input: CandidateTurnInput
    session_state: Dict[str, Any]
    session_id: UUID | str
    candidate_text: str
    is_vi: bool
    current_stage: InterviewStage
    recent_lines: List[str]
    seed: str
    now: datetime
    started_at: datetime
    elapsed_seconds: int
    target_duration_seconds: int
    time_remaining_seconds: int
    is_hard_timeout: bool
    current_question: Dict[str, Any]
    question_prompt: str
    active_prompt: str
    heard_prompt: str
    current_turn_in_question: int
    consecutive_uncooperative: int
    allow_early_exit: bool
    clarify_count: int
    blueprint: InterviewBlueprint
    # Written by the rule ladder once the answer has been read.
    eval_result: Dict[str, Any] = field(default_factory=dict)
    intent: str = ""
    sufficiency_status: str = ""
    is_sufficient: bool = True
    is_give_up: bool = False
    score_telemetry: float = 6.0
    # Resolved once per turn by the decider so ``act_*`` never re-reads the queue.
    window: Optional["ProbeWindow"] = None
    next_stage: Optional[InterviewStage] = None
    next_question: Any = None
    is_deepening_probe: bool = False

    @property
    def next_turn_index(self) -> int:
        return self.turn_input.turn_index + 1

    def apply_timing(self) -> None:
        """Write the clock into the session state (after the abort guard, as before)."""
        self.session_state["elapsed_time"] = self.elapsed_seconds
        self.session_state["remaining_time"] = self.time_remaining_seconds


def build_turn_context(turn_input: CandidateTurnInput, session_state: Dict[str, Any]) -> TurnContext:
    """Read the input and the session state once; no model call, no write."""
    session_id = turn_input.session_id
    candidate_text = turn_input.text_content.strip()
    locale = session_state.get("locale", "vi-VN")
    is_vi = str(locale).lower().startswith("vi")
    current_stage = InterviewStage(session_state.get("current_stage", InterviewStage.WARM_UP))
    recent_lines = [str(x) for x in (session_state.get("recent_interviewer_lines") or []) if x]
    seed = f"{session_id}:{turn_input.turn_index}"

    now = datetime.now(timezone.utc)
    # Session elapsed time comes from `session_state`, never from
    # `turn_input.duration_seconds` -- that field is the length of THIS
    # answer and is used only for the per-question hard-answer timeout.
    started_at = session_state.get("started_at") or now
    if isinstance(started_at, str):
        try:
            started_at = datetime.fromisoformat(started_at)
        except Exception:
            started_at = now
    if started_at is None:
        started_at = now
    if session_state.get("elapsed_time") is not None and session_state.get("elapsed_time") > 0:
        elapsed_seconds = int(session_state["elapsed_time"])
    else:
        elapsed_seconds = max(0, int((now - started_at).total_seconds()))
    target_duration_seconds = session_state.get("target_duration_minutes", 25) * 60
    time_remaining_seconds = max(0, target_duration_seconds - elapsed_seconds)

    # A demo follows its turns, not the clock: it only times out once it is
    # past its nominal duration by the overtime grace.
    if session_state.get("is_demo"):
        is_hard_timeout = elapsed_seconds > target_duration_seconds + DEMO_OVERTIME_GRACE_SECONDS
    else:
        is_hard_timeout = (
            time_remaining_seconds <= pacing_seconds(30, target_duration_seconds)
            and current_stage != InterviewStage.CLOSING
        )

    current_question = session_state.get("current_question_context", {}) or {}
    question_prompt = (
        current_question.get("follow_up_prompt")
        or current_question.get("spoken_prompt")
        or current_question.get("spokenPrompt")
        or current_question.get("main_prompt")
        or current_question.get("question")
        or ""
    )
    # After a probe the candidate is looking at the probe, so "repeat",
    # "explain" and "back to the question" refer to it, not the main question.
    active_prompt = current_question.get("follow_up_prompt") or question_prompt
    # What the candidate actually heard: the spoken version when there is one.
    heard_prompt = (
        current_question.get("follow_up_prompt")
        or current_question.get("spoken_prompt")
        or question_prompt
    )

    return TurnContext(
        turn_input=turn_input,
        session_state=session_state,
        session_id=session_id,
        candidate_text=candidate_text,
        is_vi=is_vi,
        current_stage=current_stage,
        recent_lines=recent_lines,
        seed=seed,
        now=now,
        started_at=started_at,
        elapsed_seconds=elapsed_seconds,
        target_duration_seconds=target_duration_seconds,
        time_remaining_seconds=time_remaining_seconds,
        is_hard_timeout=is_hard_timeout,
        current_question=current_question,
        question_prompt=question_prompt,
        active_prompt=active_prompt,
        heard_prompt=heard_prompt,
        current_turn_in_question=session_state.get("current_turn_in_question", 0),
        consecutive_uncooperative=session_state.get(
            "consecutive_uncooperative", session_state.get("consecutive_fails", 0)
        ),
        allow_early_exit=session_state.get("allow_early_exit", True),
        clarify_count=session_state.get("clarify_count", 0),
        blueprint=blueprint_from_payload(session_state.get("blueprint")),
    )


@dataclass(frozen=True)
class ProbeWindow:
    """Whether a follow-up is allowed on this turn, and every reason it is not."""

    open: bool
    reasons: Dict[str, bool]
    is_hard_answer_timeout: bool
    is_behind_schedule: bool
    is_near_closing_reserve: bool
    is_near_behavioral_reserve: bool
    has_pending_behavioral: bool


def probe_window(ctx: TurnContext) -> ProbeWindow:
    """The probe budget and time gates of the turn — code, shared by the rule ladder and the menu.

    Independent of whether the answer was sufficient: that is the decider's
    business. ``ctx.is_give_up`` is read because a give-up never gets a probe.
    """
    state = ctx.session_state
    blueprint = ctx.blueprint
    closing_reserve_seconds = state.get(
        "closing_reserve_seconds", pacing_seconds(blueprint.closing.reserve_seconds, ctx.target_duration_seconds)
    )
    behavioral_reserve_seconds = state.get(
        "behavioral_reserve_seconds",
        pacing_seconds(blueprint.behavioral.reserve_seconds, ctx.target_duration_seconds),
    )
    hard_answer_seconds = ctx.current_question.get("hard_answer_seconds", 180)
    duration = ctx.turn_input.duration_seconds
    is_hard_answer_timeout = duration > hard_answer_seconds if duration and duration > 0 else False
    is_near_closing_reserve = ctx.time_remaining_seconds <= closing_reserve_seconds
    is_near_behavioral_reserve = ctx.time_remaining_seconds <= (
        closing_reserve_seconds + behavioral_reserve_seconds
    ) and ctx.current_stage in [InterviewStage.VALIDATE, InterviewStage.DEEP_DIVE, InterviewStage.CHALLENGE]
    asked_ids = set(str(qid) for qid in state.get("asked_question_ids", []))
    behavioral_queue = state.get("questions_pool", {}).get(InterviewStage.BEHAVIORAL.value, [])
    has_pending_behavioral = any(
        str(q.get("question_id") or q.get("question_version_id") or "") not in asked_ids
        for q in behavioral_queue
    )
    # Nếu đang bị trễ giờ hơn 80% thời gian -> cấm probe, ép chuyển câu
    is_behind_schedule = (
        ctx.elapsed_seconds > (ctx.target_duration_seconds * blueprint.pacing.behind_schedule_ratio)
    ) and ctx.current_stage in [InterviewStage.VALIDATE, InterviewStage.DEEP_DIVE]
    stage_probes_enabled = (
        blueprint.validate_cv.probe_enabled
        if ctx.current_stage == InterviewStage.VALIDATE
        else blueprint.technical.probe_enabled
    )
    reasons = {
        # A demo asks each frozen question once so every stage fits.
        "demo": bool(state.get("is_demo")),
        "give_up": ctx.is_give_up,
        "probe_budget": ctx.current_turn_in_question >= blueprint.contract.max_probes_per_question,
        "behind_schedule": is_behind_schedule,
        "hard_answer_timeout": is_hard_answer_timeout,
        "behavioral_reserve": is_near_behavioral_reserve and has_pending_behavioral,
        "closing_reserve": is_near_closing_reserve,
        "stage": ctx.current_stage
        not in [InterviewStage.VALIDATE, InterviewStage.DEEP_DIVE, InterviewStage.CHALLENGE],
        "blueprint_probes_off": not stage_probes_enabled,
    }
    return ProbeWindow(
        open=not any(reasons.values()),
        reasons=reasons,
        is_hard_answer_timeout=is_hard_answer_timeout,
        is_behind_schedule=is_behind_schedule,
        is_near_closing_reserve=is_near_closing_reserve,
        is_near_behavioral_reserve=is_near_behavioral_reserve,
        has_pending_behavioral=has_pending_behavioral,
    )


def output_metadata_base(ctx: TurnContext) -> Dict[str, Any]:
    """The two counters every output carries (and the deprecated alias)."""
    return {
        "consecutive_uncooperative": ctx.consecutive_uncooperative,
        "consecutive_fails": ctx.consecutive_uncooperative,  # Deprecated compatibility alias.
    }
