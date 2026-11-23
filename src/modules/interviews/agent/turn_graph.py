"""The turn graph: guards_in → build_menu → decide → act → guards_out → record.

Pha 1 of INTERVIEW_AGENT_DECISION_PLAN_V1. The nodes call the core engine's
steps, so this graph and ``InterviewCoreEngine.handle_turn`` are the same
pipeline; the graph adds the menu, the pacing block and the decision record,
and is where the model's decider plugs in (Pha 2). Nothing here chooses
differently from the engine today: ``decide`` is the rule ladder.

``run_turn`` is the entry point for the application runtime. An engine that
does not expose the steps (a test double with only ``handle_turn``) is run
through ``handle_turn`` unchanged.
"""

from __future__ import annotations

from time import perf_counter
from typing import Any

from langgraph.graph import END, START, StateGraph

from src.modules.interviews.agent.menu import build_menu
from src.modules.interviews.agent.pacing import pacing_block
from src.modules.interviews.agent.record import decision_record, emit
from src.modules.interviews.agent.state import TurnGraphState
from src.modules.interviews.core.contracts.interview import CandidateTurnInput, InterviewerTurnOutput
from src.modules.interviews.core.conversation.turn_context import build_turn_context
from src.modules.interviews.core.dialogue.policy import canary_scope

MODE_RULE = "rule"


async def guards_in(state: TurnGraphState) -> dict:
    ctx = build_turn_context(state["turn_input"], state["session_state"])
    early = await state["engine"]._guards_in(ctx)
    return {"ctx": ctx, "early_decision": early, "started_at_ms": perf_counter()}


async def build_menu_node(state: TurnGraphState) -> dict:
    ctx = state["ctx"]
    menu = build_menu(ctx)
    return {"allowed_actions": menu.allowed, "menu_reasons": menu.reasons, "pacing": pacing_block(ctx)}


async def decide(state: TurnGraphState) -> dict:
    early = state.get("early_decision")
    if early is not None:
        return {"decision": early}
    return {"decision": await state["engine"]._decide_by_rule(state["ctx"])}


async def act(state: TurnGraphState) -> dict:
    output = await state["engine"]._act(state["ctx"], state["decision"])
    return {"output": output}


async def guards_out(state: TurnGraphState) -> dict:
    output = await state["engine"]._guards_out(state["output"], state["session_state"], state.get("canary_code"))
    return {"output": output}


async def record(state: TurnGraphState) -> dict:
    decision = state["decision"]
    rec = decision_record(
        mode=state.get("mode", MODE_RULE),
        decision=decision,
        allowed_actions=state.get("allowed_actions", []),
        menu_reasons=state.get("menu_reasons", {}),
        pacing=state.get("pacing", {}),
        started_at=state.get("started_at_ms", perf_counter()),
    )
    output: InterviewerTurnOutput = state["output"]
    output = output.model_copy(update={"metadata": {**(output.metadata or {}), "decision": rec}})
    emit(rec, session_id=str(output.session_id), turn_index=output.turn_index)
    return {"output": output, "record": rec}


def _after_guards(state: TurnGraphState) -> str:
    # An early decision (abort phrase, hard timeout) skips the menu: nothing to choose.
    return "act" if state.get("early_decision") is not None else "build_menu"


def build_turn_graph() -> Any:
    graph = StateGraph(TurnGraphState)
    graph.add_node("guards_in", guards_in)
    graph.add_node("build_menu", build_menu_node)
    graph.add_node("decide", decide)
    graph.add_node("act", act)
    graph.add_node("guards_out", guards_out)
    graph.add_node("record", record)
    graph.add_edge(START, "guards_in")
    graph.add_conditional_edges("guards_in", _after_guards, {"act": "decide", "build_menu": "build_menu"})
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
    mode: str = MODE_RULE,
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
            "mode": mode,
        })
    return state["output"]
