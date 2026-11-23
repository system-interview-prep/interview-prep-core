"""The turn graph: guards_in → build_menu → decide → act → guards_out → record.

Pha 1–3 of INTERVIEW_AGENT_DECISION_PLAN_V1. The nodes call the core engine's
steps, so this graph and ``InterviewCoreEngine.handle_turn`` are the same
pipeline; the graph adds the menu, the pacing block, the decision record and
the model decider.

Modes (``INTERVIEW_AGENT_MODE``):

- ``off`` / ``rule`` — the rule ladder decides; no model decider (default).
- ``shadow`` — the rule decides and executes; the model decider also runs and
  its choice is recorded next to the rule's (``agreement``), never executed.
- ``live`` — the model's choice is executed when it is in the menu, carries no
  violation and its action is in ``INTERVIEW_AGENT_LIVE_ACTIONS``; otherwise
  the rule's choice is executed and the record says so.

``run_turn`` is the entry point for the application runtime. An engine that
does not expose the steps (a test double with only ``handle_turn``) is run
through ``handle_turn`` unchanged.
"""

from __future__ import annotations

import logging
import os
from time import perf_counter
from typing import Any

from langgraph.graph import END, START, StateGraph

from src.core.trace_logging import trace_event
from src.modules.interviews.agent.decide import decide_by_model, is_violation
from src.modules.interviews.agent.menu import build_menu
from src.modules.interviews.agent.pacing import pacing_block
from src.modules.interviews.agent.record import decision_record, emit
from src.modules.interviews.agent.state import TurnGraphState
from src.modules.interviews.core.contracts.interview import CandidateTurnInput, InterviewerTurnOutput
from src.modules.interviews.core.conversation.turn_context import build_turn_context
from src.modules.interviews.core.dialogue.policy import canary_scope

logger = logging.getLogger("InterviewAgent")

MODE_RULE = "rule"
MODE_SHADOW = "shadow"
MODE_LIVE = "live"
MODES = (MODE_RULE, MODE_SHADOW, MODE_LIVE)


def agent_mode() -> str:
    value = os.getenv("INTERVIEW_AGENT_MODE", "off").strip().lower()
    if value in ("", "off", "0", "false", "no"):
        return MODE_RULE
    return value if value in MODES else MODE_RULE


def live_actions() -> set[str]:
    raw = os.getenv("INTERVIEW_AGENT_LIVE_ACTIONS", "")
    return {part.strip() for part in raw.split(",") if part.strip()}


async def guards_in(state: TurnGraphState) -> dict:
    ctx = build_turn_context(state["turn_input"], state["session_state"])
    early = await state["engine"]._guards_in(ctx)
    return {"ctx": ctx, "early_decision": early, "started_at_ms": perf_counter()}


async def build_menu_node(state: TurnGraphState) -> dict:
    ctx = state["ctx"]
    menu = build_menu(ctx, memory_enabled=state.get("memory_search") is not None)
    return {"allowed_actions": menu.allowed, "menu_reasons": menu.reasons, "pacing": pacing_block(ctx)}


async def decide(state: TurnGraphState) -> dict:
    """Rule first (it reads the answer and fills the context); the model after, when asked."""
    early = state.get("early_decision")
    if early is not None:
        return {"decision": early, "agent_decision": None, "executed": "rule"}
    ctx = state["ctx"]
    rule = await state["engine"]._decide_by_rule(ctx)
    mode = state.get("mode", MODE_RULE)
    if mode == MODE_RULE:
        return {"decision": rule, "agent_decision": None, "executed": "rule"}
    agent = None
    error = None
    try:
        agent = await decide_by_model(
            ctx, allowed_actions=state.get("allowed_actions", []), pacing=state.get("pacing", {}),
            memory_search=state.get("memory_search"),
        )
    except Exception as exc:  # noqa: BLE001 - the model decider must never cost the candidate a turn
        error = type(exc).__name__
        logger.warning("agent decide failed: %s", exc)
        trace_event("interviewer", "agent_decide_error", session_id=str(ctx.session_id), error_type=error)
    executed = "rule"
    chosen = rule
    if (
        mode == MODE_LIVE
        and agent is not None
        and is_violation(agent) is None
        and agent.action in state.get("allowed_actions", [])
        and agent.action in live_actions()
    ):
        # The model may not declare an answer sufficient the evaluator found wanting.
        if agent.action == "move_to_next_question" and agent.args.get("reason") == "sufficient" and not ctx.is_sufficient:
            agent.args["overridden"] = "sufficiency"
        else:
            chosen, executed = agent, "agent"
    return {"decision": chosen, "rule_decision": rule, "agent_decision": agent, "executed": executed,
            "decide_error": error}


async def act(state: TurnGraphState) -> dict:
    output = await state["engine"]._act(state["ctx"], state["decision"])
    return {"output": output}


async def guards_out(state: TurnGraphState) -> dict:
    output = await state["engine"]._guards_out(state["output"], state["session_state"], state.get("canary_code"))
    return {"output": output}


async def record(state: TurnGraphState) -> dict:
    rec = decision_record(
        mode=state.get("mode", MODE_RULE),
        decision=state["decision"],
        allowed_actions=state.get("allowed_actions", []),
        menu_reasons=state.get("menu_reasons", {}),
        pacing=state.get("pacing", {}),
        started_at=state.get("started_at_ms", perf_counter()),
        rule_decision=state.get("rule_decision") or (state["decision"] if state["decision"].source == "rule" else None),
        agent_decision=state.get("agent_decision"),
        executed=state.get("executed", "rule"),
        decide_error=state.get("decide_error"),
    )
    output: InterviewerTurnOutput = state["output"]
    output = output.model_copy(update={"metadata": {**(output.metadata or {}), "decision": rec}})
    emit(rec, session_id=str(output.session_id), turn_index=output.turn_index)
    return {"output": output, "record": rec}


def _after_guards(state: TurnGraphState) -> str:
    # An early decision (abort phrase, hard timeout) skips the menu: nothing to choose.
    return "decide" if state.get("early_decision") is not None else "build_menu"


def build_turn_graph() -> Any:
    graph = StateGraph(TurnGraphState)
    graph.add_node("guards_in", guards_in)
    graph.add_node("build_menu", build_menu_node)
    graph.add_node("decide", decide)
    graph.add_node("act", act)
    graph.add_node("guards_out", guards_out)
    graph.add_node("record", record)
    graph.add_edge(START, "guards_in")
    graph.add_conditional_edges("guards_in", _after_guards, {"decide": "decide", "build_menu": "build_menu"})
    graph.add_edge("build_menu", "decide")
    graph.add_edge("decide", "act")
    graph.add_edge("act", "guards_out")
    graph.add_edge("guards_out", "record")
    graph.add_edge("record", END)
    return graph.compile(name="interview_turn")


_turn_graph: Any = None


def turn_graph() -> Any:
    global _turn_graph
    if _turn_graph is None:
        _turn_graph = build_turn_graph()
    return _turn_graph


def engine_has_steps(engine: Any) -> bool:
    return all(hasattr(engine, name) for name in ("_guards_in", "_decide_by_rule", "_act", "_guards_out"))


async def run_turn(
    engine: Any,
    turn_input: CandidateTurnInput,
    session_state: dict[str, Any],
    *,
    mode: str | None = None,
    memory_search: Any = None,
) -> InterviewerTurnOutput:
    """One candidate turn through the graph; the engine's own pipeline for doubles."""
    if not engine_has_steps(engine):
        return await engine.handle_turn(turn_input, session_state)
    with canary_scope(turn_input.session_id) as code:
        state = await turn_graph().ainvoke({
            "engine": engine,
            "turn_input": turn_input,
            "session_state": session_state,
            "canary_code": code,
            "mode": mode or agent_mode(),
            "memory_search": memory_search,
        })
    return state["output"]
