"""The turn graph: guards_in → build_menu → decide → act → guards_out → record.

Pha 1–3 of INTERVIEW_AGENT_DECISION_PLAN_V1. The nodes call the core engine's
steps, so this graph and ``InterviewCoreEngine.handle_turn`` are the same
pipeline; the graph adds the menu, the pacing block, the decision record and
the model decider.

Modes (``INTERVIEW_AGENT_MODE``):

- ``rule`` (default) — the rule ladder decides; no model decider.
- ``shadow`` — the rule decides and executes; the model decider also runs and
  its choice is recorded next to the rule's (``agreement``), never executed.
- ``live`` — the model's choice executes when it is valid, in the code-built
  action menu and carries no violation; otherwise the rule path is used.

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
from src.modules.interviews.agents.interviewer.decide import decide_by_model, is_violation
from src.modules.interviews.agents.interviewer.menu import build_menu
from src.modules.interviews.agents.interviewer.pacing import pacing_block
from src.modules.interviews.agents.interviewer.record import decision_record, emit
from src.modules.interviews.agents.interviewer.state import TurnGraphState
from src.modules.interviews.core.contracts.interview import CandidateTurnInput, InterviewerTurnOutput
from src.modules.interviews.core.conversation.turn_context import build_turn_context
from src.modules.interviews.core.guardrails.common import canary_scope

logger = logging.getLogger("InterviewAgent")

MODE_RULE = "rule"
MODE_SHADOW = "shadow"
MODE_LIVE = "live"
MODES = (MODE_RULE, MODE_SHADOW, MODE_LIVE)


def agent_mode() -> str:
    value = os.getenv("INTERVIEW_AGENT_MODE", MODE_RULE).strip().lower()
    return value if value in MODES else MODE_RULE


async def guards_in(state: TurnGraphState) -> dict:
    ctx = build_turn_context(state["turn_input"], state["session_state"])
    early = await state["engine"]._guards_in(ctx)
    return {"ctx": ctx, "early_decision": early, "started_at_ms": perf_counter()}


async def build_menu_node(state: TurnGraphState) -> dict:
    ctx = state["ctx"]
    menu = build_menu(ctx, memory_enabled=state.get("memory_search") is not None)
    return {"allowed_actions": menu.allowed, "menu_reasons": menu.reasons, "pacing": pacing_block(ctx)}


async def decide(state: TurnGraphState) -> dict:
    """Use the model as the live decider; run the legacy ladder only as fallback.

    Shadow deliberately evaluates both paths. Rule mode remains the explicit
    fallback/rollback mode.
    """
    early = state.get("early_decision")
    if early is not None:
        return {"decision": early, "agent_decision": None, "executed": "rule"}
    ctx = state["ctx"]
    mode = state.get("mode", MODE_RULE)
    if mode == MODE_RULE:
        rule = await state["engine"]._decide_by_rule(ctx)
        return {"decision": rule, "agent_decision": None, "executed": "rule"}

    agent = None
    error = None

    async def ask_agent() -> None:
        nonlocal agent, error
        try:
            agent = await decide_by_model(
                ctx,
                allowed_actions=state.get("allowed_actions", []),
                pacing=state.get("pacing", {}),
                memory_search=state.get("memory_search"),
            )
        except Exception as exc:  # noqa: BLE001 - the model decider must never cost the candidate a turn
            error = type(exc).__name__
            logger.warning("agent decide failed: %s", exc)
            trace_event("interviewer", "agent_decide_error", session_id=str(ctx.session_id), error_type=error)

    if mode == MODE_SHADOW:
        rule = await state["engine"]._decide_by_rule(ctx)
        await ask_agent()
        return {
            "decision": rule,
            "rule_decision": rule,
            "agent_decision": agent,
            "executed": "rule",
            "decide_error": error,
        }

    # MODE_LIVE: deliberately call the new decider before the legacy rule
    # ladder, so its evaluation cannot steer or overrule an accepted decision.
    await ask_agent()

    if mode == MODE_LIVE:
        if (
            agent is not None
            and is_violation(agent) is None
            and agent.action in state.get("allowed_actions", [])
        ):
            # Live decisions do not consume the legacy evaluator's answer,
            # sufficiency, intent or probe text. Keep only the code-computed
            # queue/window controls needed by the action handlers.
            ctx.eval_result = {}
            ctx.intent = ""
            ctx.sufficiency_status = ""
            ctx.is_sufficient = True
            ctx.is_give_up = False
            ctx.score_telemetry = 6.0
            ctx.is_deepening_probe = False
            return {
                "decision": agent,
                "rule_decision": None,
                "agent_decision": agent,
                "executed": "agent",
                "decide_error": error,
            }

        # A malformed or rejected model action falls back to the rule path.
        # Its legacy evaluation cannot influence a valid live action.
        rule = await state["engine"]._decide_by_rule(ctx)
        return {
            "decision": rule,
            "rule_decision": rule,
            "agent_decision": agent,
            "executed": "rule",
            "decide_error": error,
        }

    # Defensive fallback for unknown modes passed directly by an internal caller.
    rule = await state["engine"]._decide_by_rule(ctx)
    return {
        "decision": rule,
        "rule_decision": rule,
        "agent_decision": agent,
        "executed": "rule",
        "decide_error": error,
    }


async def act(state: TurnGraphState) -> dict:
    output = await state["engine"]._act(state["ctx"], state["decision"])
    return {"output": output}


async def guards_out(state: TurnGraphState) -> dict:
    output = await state["engine"]._guards_out(state["output"], state["session_state"], state.get("canary_code"))
    return {"output": output}


async def record(state: TurnGraphState) -> dict:
    ctx = state["ctx"]
    contract = ctx.session_state.get("interview_contract") or {}
    next_stage = getattr(ctx, "next_stage", None)
    stage_after = getattr(state["output"].current_stage, "value", state["output"].current_stage)
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
        contract={
            "reference": contract.get("reference"),
            "session_plan_fingerprint": contract.get("session_plan_fingerprint"),
            "coverage_target": contract.get("coverage_target"),
        },
        stage_before=ctx.current_stage.value,
        stage_after=str(stage_after),
        transition_target=(
            getattr(next_stage, "value", next_stage)
            if state["decision"].action == "move_to_next_question"
            else None
        ),
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
    # This is volatile working state (it contains the live engine and context).
    # It runs inside the durable command graph, so None would inherit the
    # parent's PostgreSQL checkpointer and try to msgpack-serialize the engine.
    return graph.compile(checkpointer=False, name="interview_turn")


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
        state = await turn_graph().ainvoke(
            {
                "engine": engine,
                "turn_input": turn_input,
                "session_state": session_state,
                "canary_code": code,
                "mode": mode or agent_mode(),
                "memory_search": memory_search,
            },
            # The parent command graph uses sync durability; explicitly avoid
            # inheriting it because this graph has no checkpointer of its own.
            durability="async",
        )
    return state["output"]
