"""The pacing block: where the interview stands against its plan, as numbers.

Time is never handed to the model (INTERVIEW_AGENT_DECISION_PLAN_V1 §4.5): the
cut-offs and the bans are code. But the decider must *see* the clock and the
plan — how many questions remain, whether the pace is behind — to trade a probe
for coverage, or to use spare minutes for evidence. Everything here is computed
from the session state; nothing is estimated by a model.
"""

from __future__ import annotations

from typing import Any, Dict

from src.modules.interviews.core.contracts.interview import InterviewStage
from src.modules.interviews.core.conversation.turn_context import TurnContext, probe_limit, question_time_budget
from src.modules.interviews.core.conversation.turn_rules import TIME_THRESHOLDS

# `behind` when the remaining plan needs more than this share over the time left;
# `ahead` when it needs less than one minus this share.
BEHIND_RATIO = 1.15
AHEAD_RATIO = 0.75
# Before any question was answered, assume this many seconds per question.
DEFAULT_SECONDS_PER_QUESTION = 120

ASSESSED_STAGES = (
    InterviewStage.VALIDATE,
    InterviewStage.DEEP_DIVE,
    InterviewStage.CHALLENGE,
    InterviewStage.BEHAVIORAL,
)


def questions_remaining_by_stage(session_state: Dict[str, Any]) -> Dict[str, int]:
    """Frozen questions not yet asked, per stage (warm-up and closing excluded)."""
    asked = {str(q) for q in session_state.get("asked_question_ids", []) or []}
    pool = session_state.get("questions_pool", {}) or {}
    remaining: Dict[str, int] = {}
    for stage in ASSESSED_STAGES:
        queue = pool.get(stage.value, []) or []
        left = sum(
            1
            for q in queue
            if str(q.get("question_id") or q.get("question_version_id") or "") not in asked
        )
        if left:
            remaining[stage.value] = left
    return remaining


def competency_coverage(session_state: Dict[str, Any]) -> list[Dict[str, Any]]:
    """Question coverage from the frozen queue; it does not claim answer quality."""
    pool = session_state.get("questions_pool", {}) or {}
    target_by_id = {
        str(t.get("conceptId") or ""): t
        for t in session_state.get("contract_targets", []) or []
        if t.get("conceptId")
    }
    groups: Dict[str, Dict[str, Any]] = {}
    settled = {"ANSWERED", "COMPLETED", "EVALUATED"}
    for stage in ASSESSED_STAGES:
        for question in pool.get(stage.value, []) or []:
            key = str(question.get("concept_id") or question.get("competency") or "unknown")
            entry = groups.setdefault(key, {
                "competency": question.get("competency") or key,
                "concept_id": question.get("concept_id") or key,
                "planned": 0,
                "asked": 0,
                "remaining": 0,
                "status": "NOT_STARTED",
            })
            entry["planned"] += 1
            status = str(question.get("status") or "PLANNED").upper()
            if status in settled:
                entry["asked"] += 1
            elif status == "ASKED":
                entry["asked"] += 1
            else:
                entry["remaining"] += 1
            target = target_by_id.get(key, {})
            if target:
                priorities = target.get("rationale", {}).get("priorities", []) or []
                priority_text = " ".join(str(value).lower() for value in priorities)
                entry["priority"] = "MUST" if "must" in priority_text else "PLANNED"
    for entry in groups.values():
        if entry["asked"]:
            entry["status"] = "IN_PROGRESS" if entry["remaining"] else "QUESTIONS_ASKED"
    return sorted(groups.values(), key=lambda row: (row.get("priority") != "MUST", row["competency"]))


def pacing_block(ctx: TurnContext) -> Dict[str, Any]:
    """The block the decider receives every turn; also stored in the decision record."""
    state = ctx.session_state
    pool = state.get("questions_pool", {}) or {}
    planned = sum(len(pool.get(stage.value, []) or []) for stage in ASSESSED_STAGES)
    remaining = questions_remaining_by_stage(state)
    coverage = competency_coverage(state)
    remaining_total = sum(remaining.values())
    asked = max(0, planned - remaining_total)
    target = max(1, ctx.target_duration_seconds)
    elapsed_ratio = round(min(1.0, ctx.elapsed_seconds / target), 3)

    if asked > 0:
        seconds_per_question = int(ctx.elapsed_seconds / asked)
    else:
        seconds_per_question = DEFAULT_SECONDS_PER_QUESTION
    seconds_needed = remaining_total * seconds_per_question

    if state.get("is_demo") or remaining_total == 0:
        # A demo is turn-driven; with nothing left there is nothing to pace.
        pace = "on_track"
    elif seconds_needed > ctx.time_remaining_seconds * BEHIND_RATIO:
        pace = "behind"
    elif seconds_needed < ctx.time_remaining_seconds * AHEAD_RATIO:
        pace = "ahead"
    else:
        pace = "on_track"

    probes_used = int(ctx.current_turn_in_question)
    budget = question_time_budget(ctx)
    return {
        "elapsed_ratio": elapsed_ratio,
        "time_remaining_seconds": int(ctx.time_remaining_seconds),
        "stage": ctx.current_stage.value,
        "stage_until_ratio": TIME_THRESHOLDS.get(ctx.current_stage, 1.0),
        "questions_asked": asked,
        "questions_planned": planned,
        "questions_remaining_by_stage": remaining,
        "competency_coverage": coverage,
        "coverage_target": (state.get("blueprint") or {}).get("contract", {}).get("coverageTarget", "all_frozen_questions"),
        "seconds_per_question_so_far": seconds_per_question,
        "seconds_needed_for_remaining_plan": seconds_needed,
        "pace": pace,
        "probes_used_in_question": probes_used,
        "probes_left_in_question": max(0, probe_limit(ctx) - probes_used),
        # 3bf4591 told the model to "continue while useful and within interview
        # time"; session 2678ce7b took that literally (nine probes, one question).
        # The model now sees the question's own share and what it has used.
        "question_seconds_used": budget[0] if budget else None,
        "question_seconds_budget": budget[1] if budget else None,
    }
