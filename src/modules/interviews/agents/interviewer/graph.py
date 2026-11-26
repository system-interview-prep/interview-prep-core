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

import asyncio
import logging
import os
from time import perf_counter
from typing import Any

from langgraph.graph import END, START, StateGraph

from src.core.trace_logging import trace_event
from src.core.config import get_settings
from src.modules.interviews.agents.interviewer.arbiter import arbitrate
from src.modules.interviews.agents.interviewer.menu import build_menu
from src.modules.interviews.agents.interviewer.pacing import pacing_block
from src.modules.interviews.agents.interviewer.record import decision_record, emit
from src.modules.interviews.agents.interviewer.router import FAST, HYBRID, choose_path
from src.modules.interviews.agents.interviewer.state import TurnGraphState
from src.modules.interviews.agents.interviewer.talker import predict_by_talker, talker_model
from src.modules.interviews.agents.interviewer.thinker import decide_by_model, is_violation
from src.modules.interviews.core.conversation.turn_context import Decision
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


def _decision_from_preview(record: dict) -> Decision | None:
    """Rebuild the decision a validated preview carried (V2 spec §7, Fast path).

    The voice runtime sets ``precomputed_decision`` only after the server has
    confirmed the final transcript and the active turn match what the preview
    assumed, so this was computed on the exact text the candidate finished
    with -- not a partial transcript. It is still re-checked against the menu
    below like any live decision, and marked as having come from a preview.
    """
    try:
        action = str(record.get("action") or "")
        if not action:
            return None
        return Decision(
            action,
            dict(record.get("args") or {}),
            reason=str(record.get("reason") or "preview"),
            evidence_refs=[str(r) for r in (record.get("evidence_refs") or [])],
            source="agent",
            path=["preview", *[str(x) for x in (record.get("path") or [])]],
        )
    except Exception:  # noqa: BLE001 - a malformed preview is just "no preview"
        return None


async def route(state: TurnGraphState) -> dict:
    """Pick the execution path (V2 spec §6.1). Deterministic; costs no call.

    Only LIVE routes. RULE and SHADOW are rollout modes and keep their own
    behaviour untouched; the path recorded for them is the mode itself so the
    record never shows a path those modes did not take.
    """
    mode = state.get("mode", MODE_RULE)
    if mode != MODE_LIVE:
        return {"execution_path": mode, "route_reason": "rollout_mode"}
    settings = get_settings()
    turn_input = state["turn_input"]
    chosen = choose_path(
        early_decision=state.get("early_decision"),
        preview_decision=getattr(turn_input, "precomputed_decision", None),
        modality=getattr(turn_input, "modality", "CHAT"),
        hybrid_enabled=bool(settings.interview_talker_enabled),
        talker_model=talker_model(),
    )
    return {"execution_path": chosen.path, "route_reason": chosen.reason}


async def decide(state: TurnGraphState) -> dict:
    """Use the model as the live decider; run the legacy ladder only as fallback.

    Shadow deliberately evaluates both paths. Rule mode remains the explicit
    fallback/rollback mode. Inside LIVE the execution path chosen by ``route``
    decides how many calls are made: Fast reuses a validated preview (0 new),
    Unified asks the Thinker once (1), Hybrid asks the Thinker and the Talker
    concurrently (2 in parallel) and lets the Arbiter attach the Talker's line
    only when it can prove equivalence.
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
    talker = None
    arbiter: dict | None = None
    allowed = state.get("allowed_actions", [])

    # The question that moving on would ask, resolved before the decider
    # writes its bridge (session 1b264b67: bridges were written blind, so
    # they summed up the warm-up instead of leading somewhere). The same
    # selection act_move_to_next_question would make; it changes no state.
    next_question_text = ""
    if "move_to_next_question" in allowed and ctx.next_question is None:
        try:
            ctx.next_stage, ctx.next_question = state["engine"]._get_next_stage_and_question(ctx.session_state)
        except Exception:  # noqa: BLE001 - without it the bridge is written as before
            ctx.next_stage, ctx.next_question = None, None
    if ctx.next_question is not None:
        next_question_text = str(
            getattr(ctx.next_question, "spoken_prompt", None) or getattr(ctx.next_question, "main_prompt", "") or ""
        )

    async def ask_agent() -> None:
        nonlocal agent, error
        try:
            agent = await decide_by_model(
                ctx,
                allowed_actions=allowed,
                pacing=state.get("pacing", {}),
                memory_search=state.get("memory_search"),
                next_question=next_question_text,
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

    path = state.get("execution_path")

    # ---- Fast: a validated preview already holds the decision -------------
    if path == FAST:
        preview = getattr(state["turn_input"], "precomputed_decision", None)
        agent = _decision_from_preview(preview) if preview else None
        if agent is None:
            # Nothing reusable after all: pay for the Unified call.
            trace_event("interviewer", "fast_path_fell_through", session_id=str(ctx.session_id))
            await ask_agent()

    # ---- Hybrid: Thinker and Talker concurrently, early speech before fan-in
    elif path == HYBRID:
        thinker_task = asyncio.create_task(ask_agent())
        talker_task = asyncio.create_task(
            predict_by_talker(ctx, allowed_actions=allowed, pacing=state.get("pacing", {}))
        )
        # Completion order, recorded exactly. Inferring "who finished first"
        # from asyncio.wait's return set is wrong when both land in one loop
        # tick (Windows timers resolve at ~15 ms): the set holds both, the
        # Thinker looks "done first", and a perfectly good acknowledgment is
        # dropped although nothing has been spoken yet. The callbacks fire in
        # the order the tasks actually completed.
        finished: list[asyncio.Task] = []
        thinker_task.add_done_callback(finished.append)
        talker_task.add_done_callback(finished.append)
        try:
            await asyncio.wait({thinker_task, talker_task}, return_when=asyncio.FIRST_COMPLETED)
            if (
                talker_task.done()
                and not talker_task.cancelled()
                and (not finished or finished[0] is talker_task)
            ):
                # The Talker got there first: say its neutral line now, while
                # the Thinker is still deciding (or has only just finished and
                # nothing has been spoken). §6.3: only after the turn ended and
                # the input guards passed (both true here) and only a line that
                # already passed the acknowledgement guard (talker.py does that).
                talker = talker_task.result()
                speak = state.get("on_early_speech")
                if talker is not None and talker.acknowledgment and speak is not None:
                    try:
                        speak(talker.acknowledgment)
                        trace_event("interviewer", "early_speech", session_id=str(ctx.session_id))
                    except Exception:  # noqa: BLE001 - early speech is best effort
                        logger.debug("early speech failed", exc_info=True)
            await thinker_task
            if talker is None and talker_task.done() and not talker_task.cancelled():
                talker = talker_task.result()
        finally:
            # Structured cleanup: whatever path led here -- the Thinker
            # answered first, this turn was cancelled from outside, or a
            # callback raised -- no child task outlives the decision. §6.4:
            # Luna is not waited for at fan-in; a cancelled client call may
            # still be billed, which the record counts as a wasted call.
            for task in (thinker_task, talker_task):
                if not task.done():
                    task.cancel()
            if not talker_task.done() or talker_task.cancelled():
                trace_event("interviewer", "talker_cancelled_at_fan_in", session_id=str(ctx.session_id))

    # ---- Unified: the single Thinker call ---------------------------------
    else:
        await ask_agent()

    if mode == MODE_LIVE:
        if (
            agent is not None
            and is_violation(agent) is None
            and agent.action in allowed
        ):
            if path == HYBRID:
                verdict = arbitrate(ctx, thinker=agent, talker=talker, allowed_actions=allowed)
                agent = verdict.decision
                arbiter = {
                    "talker_reused": verdict.talker_reused,
                    "outcome": verdict.talker_outcome,
                    "early_acknowledgment": verdict.acknowledgment,
                    "talker_model": getattr(talker, "model", None),
                }
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
                "talker_output": talker,
                "arbiter": arbiter,
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
            "talker_output": talker,
            "arbiter": arbiter,
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
        execution_path=state.get("execution_path"),
        route_reason=state.get("route_reason"),
        arbiter=state.get("arbiter"),
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
    graph.add_node("route", route)
    graph.add_node("decide", decide)
    graph.add_node("act", act)
    graph.add_node("guards_out", guards_out)
    graph.add_node("record", record)
    graph.add_edge(START, "guards_in")
    graph.add_conditional_edges("guards_in", _after_guards, {"decide": "route", "build_menu": "build_menu"})
    graph.add_edge("build_menu", "route")
    graph.add_edge("route", "decide")
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
    on_early_speech: Any = None,
) -> InterviewerTurnOutput:
    """One candidate turn through the graph; the engine's own pipeline for doubles.

    ``on_early_speech(text)`` is called on the Hybrid path with the Talker's
    guarded acknowledgment as soon as it is available, before the graph has
    finished -- the §6.4 early response that does not wait for fan-in. A
    caller that cannot speak passes nothing and the line is simply not said.
    """
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
                "on_early_speech": on_early_speech,
            },
            # The parent command graph uses sync durability; explicitly avoid
            # inheriting it because this graph has no checkpointer of its own.
            durability="async",
        )
    return state["output"]
