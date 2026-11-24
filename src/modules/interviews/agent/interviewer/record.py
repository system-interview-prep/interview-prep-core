"""The decision record: why this turn went the way it did, as data.

One record per turn, attached to ``output.metadata["decision"]`` and emitted as
the ``agent_decision`` pipeline event, so a reviewer or the admin pipeline view
can read the menu, the pace, the rule's choice and the model's choice without
Langfuse (INTERVIEW_AGENT_DECISION_PLAN_V1 §5.2).
"""

from __future__ import annotations

from time import perf_counter
from typing import Any, Dict, Optional

from src.core.trace_logging import trace_event
from src.modules.interviews.core.conversation.turn_context import Decision


def decision_record(
    *,
    mode: str,
    decision: Decision,
    allowed_actions: list[str],
    menu_reasons: Dict[str, str],
    pacing: Dict[str, Any],
    started_at: float,
    rule_decision: Optional[Decision] = None,
    agent_decision: Optional[Decision] = None,
    executed: str = "rule",
    decide_error: Optional[str] = None,
    contract: Optional[Dict[str, Any]] = None,
    stage_before: Optional[str] = None,
    stage_after: Optional[str] = None,
    transition_target: Optional[str] = None,
) -> Dict[str, Any]:
    violation = None
    if agent_decision is not None and agent_decision.source == "agent":
        violation = agent_decision.args.get("violation")
    agreement = None
    if agent_decision is not None and rule_decision is not None and violation is None:
        agreement = agent_decision.action == rule_decision.action
    changed_stage = bool(stage_before and stage_after and stage_before != stage_after)
    decision_source = "agent" if executed == "agent" else "rule"
    agent_validation = "not_run"
    if agent_decision is not None:
        if agent_decision.args.get("violation"):
            agent_validation = f"rejected:{agent_decision.args['violation']}"
        elif agent_decision.action not in allowed_actions:
            agent_validation = "rejected:outside_menu"
        elif executed == "agent":
            agent_validation = "accepted_and_executed"
        else:
            agent_validation = "accepted_but_not_executed"
    transition = {
        "from_stage": stage_before,
        "to_stage": stage_after if changed_stage else stage_before,
        "proposed_stage": transition_target,
        "changed": changed_stage,
        "reason": decision.reason or decision.args.get("reason") or "no_transition",
        "selected_by": "code_fsm" if decision.action == "move_to_next_question" else decision_source,
        "validation": agent_validation if agent_decision is not None else "rule_path",
    }
    return {
        "mode": mode,
        "executed": executed,
        "allowed_actions": list(allowed_actions),
        "menu_reasons": dict(menu_reasons),
        "in_menu": decision.action in allowed_actions,
        "rule": rule_decision.as_record() if rule_decision is not None else None,
        "agent": agent_decision.as_record() if agent_decision is not None else None,
        "agreement": agreement,
        "menu_violation": violation,
        "decide_error": decide_error,
        "pacing": dict(pacing),
        "contract": dict(contract or {}),
        "stage_before": stage_before,
        "stage_after": stage_after,
        "transition": transition,
        "latency_ms": int((perf_counter() - started_at) * 1000),
    }


def summary(record: Dict[str, Any]) -> Dict[str, Any]:
    """The short form for the turn_spoken payload and the admin view."""
    rule = record.get("rule") or {}
    agent = record.get("agent") or {}
    return {
        "mode": record.get("mode"),
        "executed": record.get("executed"),
        "rule_action": rule.get("action"),
        "agent_action": agent.get("action") or None,
        "agent_reason": str(agent.get("reason") or "")[:200] or None,
        "agreement": record.get("agreement"),
        "menu_violation": record.get("menu_violation"),
        "decide_error": record.get("decide_error"),
        "pace": (record.get("pacing") or {}).get("pace"),
        "in_menu": record.get("in_menu"),
        "contract_ref": (record.get("contract") or {}).get("reference"),
        "stage_before": record.get("stage_before"),
        "stage_after": record.get("stage_after"),
        "transition": record.get("transition"),
        "agent_validation": record.get("transition", {}).get("validation"),
        "latency_ms": record.get("latency_ms"),
    }


def scores(record: Dict[str, Any]) -> Dict[str, float]:
    """Numeric Langfuse scores of the turn (``agent.*``)."""
    out: Dict[str, float] = {"agent.latency_ms": float(record.get("latency_ms") or 0)}
    if record.get("agreement") is not None:
        out["agent.agreement"] = 1.0 if record["agreement"] else 0.0
    out["agent.menu_violation"] = 1.0 if record.get("menu_violation") else 0.0
    out["agent.decide_error"] = 1.0 if record.get("decide_error") else 0.0
    out["agent.executed_by_agent"] = 1.0 if record.get("executed") == "agent" else 0.0
    return out


def emit(record: Dict[str, Any], *, session_id: Any, turn_index: int) -> None:
    """Persist the record as a pipeline event (events with a session id are stored)."""
    trace_event(
        "interviewer",
        "agent_decision",
        session_id=session_id,
        turn_index=turn_index,
        **summary(record),
        allowed_actions=record["allowed_actions"],
        rule=record.get("rule"),
        agent=record.get("agent"),
        pacing=record.get("pacing"),
        contract=record.get("contract"),
    )
