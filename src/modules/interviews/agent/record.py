"""The decision record: why this turn went the way it did, as data.

One record per turn, attached to ``output.metadata["decision"]`` and emitted as
the ``agent_decision`` pipeline event, so a reviewer or the admin pipeline view
can read the menu, the pace and the chosen action without Langfuse
(INTERVIEW_AGENT_DECISION_PLAN_V1 §5.2).
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
    agent_decision: Optional[Decision] = None,
) -> Dict[str, Any]:
    executed = "rule" if decision.source == "rule" else "agent"
    record: Dict[str, Any] = {
        "mode": mode,
        "executed": executed,
        "allowed_actions": list(allowed_actions),
        "menu_reasons": dict(menu_reasons),
        "in_menu": decision.action in allowed_actions,
        "rule": decision.as_record() if decision.source == "rule" else None,
        "agent": agent_decision.as_record() if agent_decision is not None else None,
        "agreement": (agent_decision.action == decision.action) if agent_decision is not None else None,
        "pacing": dict(pacing),
        "latency_ms": int((perf_counter() - started_at) * 1000),
    }
    if decision.source != "rule":
        record["agent"] = decision.as_record()
    return record


def emit(record: Dict[str, Any], *, session_id: Any, turn_index: int) -> None:
    """Persist the record as a pipeline event (events with a session id are stored)."""
    trace_event(
        "interviewer",
        "agent_decision",
        session_id=session_id,
        turn_index=turn_index,
        mode=record["mode"],
        executed=record["executed"],
        action=(record.get("rule") or record.get("agent") or {}).get("action"),
        in_menu=record["in_menu"],
        pace=(record.get("pacing") or {}).get("pace"),
        allowed_actions=record["allowed_actions"],
        agreement=record.get("agreement"),
        latency_ms=record["latency_ms"],
    )
