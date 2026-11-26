"""The model decider: one tool call that picks an action from the menu.

``decide_by_model`` never raises on a bad answer from the model; it returns a
``Decision`` with ``source="agent"`` and, when the answer was unusable, a
``violation`` naming why (``outside_menu``, ``parse``, ``multi_call`` is only
noted, ``memory_loop``). The graph treats a violation as "fall back to the
rule" and counts it (INVERVIEW_AGENT_DECISION_PLAN_V1 §6.6).

The optional memory step: the model may call ``search_candidate_memory`` once;
the hits are appended to the input and the model is asked again, at most
``max_steps`` calls in all.
"""

from __future__ import annotations

import json
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence

from src.core.trace_logging import trace_event
from src.modules.ai.facade import generate_tool_calls
from src.modules.interviews.agents.interviewer.tools import (
    SEARCH_MEMORY,
    UNIFIED_TEXT_ACTIONS,
    WARM_UP_EXIT_TEXT,
    tools_for,
    unified_enabled,
)
from src.modules.interviews.core.contracts.interview import InterviewStage
from src.modules.interviews.core.conversation.turn_rules import warm_up_follow_up_repeats
from src.modules.interviews.core.guardrails.input import mentions_stopping, mentions_the_system
from src.modules.interviews.core.guardrails.output import (
    acknowledgement_rejection_reason,
    announces_process,
    checked_probe,
    clarify_rejection_reason,
    qna_rejection_reason,
    redirect_rejection_reason,
    validate_probe_text,
)
from src.modules.interviews.agents.interviewer.prompts.decide import decide_input, decide_prompt
from src.modules.interviews.core.conversation.turn_context import Decision, TurnContext, deepening_allowed

PURPOSE = "interviews.agent.decide"
MemorySearch = Callable[[str], Awaitable[Sequence[Dict[str, Any]]]]
ToolCaller = Callable[..., Awaitable[List[Dict[str, Any]]]]


def _checked_unified_text(
    ctx: TurnContext, *, action: str, argument: str, raw: Any
) -> Optional[str]:
    """The utterance the model wrote, if that action's own guard accepts it.

    §6.2 lets one inference carry the response, but it does not relax what the
    response has to be. Each action keeps the guard its separately generated
    line has always passed, so unifying cannot widen what reaches a candidate.
    """
    text_value = str(raw or "").strip()
    if not text_value:
        return None
    question = ctx.active_prompt or ctx.question_prompt
    rubric = str(ctx.current_question.get("rubric_criteria") or "")

    if argument == "probe_text":
        return checked_probe(text_value, question=question, answer=ctx.candidate_text, rubric=rubric)

    if argument == "clarify_text":
        reason = clarify_rejection_reason(
            text_value, question=question, answer=ctx.candidate_text, rubric=rubric
        )
    elif argument == "answer_text":
        job_context = str(
            ctx.session_state.get("job_description_text")
            or ctx.session_state.get("job_context")
            or ""
        )
        reason = qna_rejection_reason(
            text_value,
            job_context=job_context,
            # The same leak predicate the engine passes for the closing Q&A.
            rubric_leak=lambda value: not validate_probe_text(value),
            question=ctx.candidate_text,
        )
    elif argument == "redirect_text":
        # A nudge back to the pending question: it must not grade the candidate
        # or hand over the answer, but it may *contain* the question, which is
        # why this is not the acknowledgement guard. Measuring showed that one
        # rejecting three of four live redirects on "is_question" alone.
        reason = redirect_rejection_reason(
            text_value, answer=ctx.candidate_text, question=question, rubric=rubric
        )
    else:  # pragma: no cover - UNIFIED_TEXT_ACTIONS and this dispatch agree
        reason = "unknown_argument"

    if reason:
        # The text is logged (truncated) so a rejection can be judged: the
        # measured run showed every remaining rejection is hints_answer, and
        # without the line nobody can tell a real leak from a redirect that
        # merely restates the question's own topic. Precedent: probe_rejected
        # already logs 200 chars of the probe.
        trace_event(
            "interviewer",
            "unified_text_rejected",
            action=action,
            argument=argument,
            reason=reason,
            text=text_value[:160],
            session_id=str(ctx.session_id),
        )
        return None
    return text_value


def _violation(kind: str, detail: str = "", calls: Optional[List[Dict[str, Any]]] = None) -> Decision:
    d = Decision("", {}, reason=detail, source="agent", path=["model"])
    d.args["violation"] = kind
    if calls:
        d.args["raw"] = calls[:3]
    return d


async def decide_by_model(
    ctx: TurnContext,
    *,
    allowed_actions: List[str],
    pacing: Dict[str, Any],
    generate: Optional[ToolCaller] = None,
    memory_search: Optional[MemorySearch] = None,
    max_steps: int = 2,
    reasoning_effort: str | None = "none",
    unified: Optional[bool] = None,
    next_question: str = "",
) -> Decision:
    # Resolved at call time so a test can route the decider through a scripted caller.
    generate = generate or generate_tool_calls
    unified = unified_enabled() if unified is None else unified
    in_warm_up = ctx.current_stage == InterviewStage.WARM_UP
    tools = tools_for(allowed_actions, unified=unified, bridge=True)
    # §6.2 "Prefetch memory trước call": the runtime already loads this session's
    # candidate memory into the state before the turn runs, so hand it to the
    # model up front. ``search_candidate_memory`` then stays what the spec calls
    # a "tool call bổ sung chỉ khi cần" -- an extra round trip only when the
    # model wants a skill the prefetch did not cover.
    prefetched: List[Dict[str, Any]] = [
        dict(hit) for hit in (ctx.session_state.get("candidate_memory") or ()) if isinstance(hit, dict)
    ]
    memory_hits: List[Dict[str, Any]] = list(prefetched)
    notes: List[str] = []
    main_question = str(
        ctx.current_question.get("spoken_prompt") or ctx.current_question.get("main_prompt") or ""
    ).strip()
    following_up = not in_warm_up and bool(ctx.question_follow_ups) and bool(ctx.question_exchange)
    items: Any = decide_input(
        question=ctx.active_prompt or ctx.question_prompt,
        candidate_text=ctx.candidate_text,
        stage=ctx.current_stage.value,
        evaluation=ctx.eval_result or None,
        assessment_criteria=str(ctx.current_question.get("rubric_criteria") or ""),
        recent_lines=ctx.recent_lines,
        memory_hits=prefetched,
        pacing=pacing,
        allowed_actions=allowed_actions,
        interview_contract=ctx.session_state.get("interview_contract"),
        is_vi=ctx.is_vi,
        warm_up_exchange=(
            {
                "follow_ups": list(ctx.warm_up_follow_ups),
                "fragments": [p.strip() for p in ctx.candidate_text.split("\n") if p.strip()],
                "used": int(ctx.clarify_count or 0),
                "limit": int(getattr(ctx.blueprint.warm_up, "max_follow_ups", 2)),
            }
            if in_warm_up
            else None
        ),
        question_exchange=(
            {"main_question": main_question, "messages": ctx.question_exchange,
             "probes": len(ctx.question_follow_ups)}
            if following_up
            else None
        ),
        next_question=next_question,
    )
    history: List[Dict[str, Any]] = [{"role": "user", "content": items}]
    for step in range(max_steps):
        calls = await generate(
            instructions=decide_prompt(ctx.is_vi),
            input_items=history if step else items,
            tools=tools,
            tool_choice="required",
            purpose=PURPOSE,
            reasoning_effort=reasoning_effort,
        )
        if not calls:
            return _violation("parse", "no tool call")
        if len(calls) > 1:
            notes.append("multi_call")
        call = calls[0]
        name = str(call.get("name") or "")
        args = dict(call.get("arguments") or {})
        if name == SEARCH_MEMORY:
            if memory_search is None or SEARCH_MEMORY not in allowed_actions:
                return _violation("outside_menu", name, calls)
            if step == max_steps - 1:
                return _violation("memory_loop", f"{max_steps} steps", calls)
            hits = list(await memory_search(str(args.get("skill_key") or "")))
            memory_hits.extend(hits)
            history.append({"type": "function_call", "name": name, "call_id": call.get("call_id", ""),
                            "arguments": json.dumps(args, ensure_ascii=False)})
            history.append({"type": "function_call_output", "call_id": call.get("call_id", ""),
                            "output": json.dumps(hits, ensure_ascii=False)})
            continue
        if name not in allowed_actions:
            return _violation("outside_menu", name, calls)
        if name == "decline_system_probe" and not mentions_the_system(ctx.candidate_text):
            # Its line is fixed ("Mình không thể chia sẻ thông tin nội bộ."), so a
            # wrong pick is heard verbatim: session 08a2ee58 answered "em đã
            # trình bày rồi" with it. The input guard catches real probes before
            # any model; the model's own pick needs the candidate to have said
            # something about the system at all.
            return _violation("unsupported_system_probe", "no mention of the system", calls)
        if name == "confirm_abort" and not mentions_stopping(ctx.candidate_text):
            # "Bạn có muốn kết thúc buổi phỏng vấn không?" after a real answer
            # (session 1b264b67). Only the candidate starts an ending.
            return _violation("unsupported_abort", "no mention of stopping", calls)
        if "_raw" in args:
            return _violation("parse", "arguments were not JSON", calls)
        reason = str(args.pop("rationale", "") or "").strip()
        refs = args.pop("evidence_refs", None)
        if not reason:
            return _violation("parse", "missing rationale", calls)
        if name == "continue_warm_up":
            follow_up = checked_probe(
                str(args.get("follow_up_text") or ""),
                question=ctx.question_prompt,
                answer=ctx.candidate_text,
            )
            if not follow_up:
                return _violation("invalid_follow_up", "missing or rejected warm-up utterance", calls)
            # The same ask again, reworded, is how session e52c0757 looped:
            # the candidate answered it already, so the warm-up is over
            # (turn_rules reads ``warm_up_repeat``) and the rule moves on.
            repeat = warm_up_follow_up_repeats(follow_up, ctx.warm_up_follow_ups)
            if repeat:
                ctx.warm_up_repeat = repeat
                trace_event(
                    "interviewer", "warm_up_follow_up_repeated", source="decision", repeat=repeat,
                    text=follow_up[:160], session_id=str(ctx.session_id),
                )
                return _violation("repeated_follow_up", repeat, calls)
            args["follow_up_text"] = follow_up
        if name == "acknowledge_and_probe" and args.get("kind") == "deepen" and not deepening_allowed(ctx):
            # Pushing on a sufficient answer is the challenge tier's move; for
            # anyone else the decider's point is a gap, and it is asked as one.
            args["kind"] = "gap"
            notes.append("deepen_downgraded")
        if name == "move_to_next_question" and WARM_UP_EXIT_TEXT in args:
            # The bridge out of the warm-up: one plain sentence or nothing.
            bridge = str(args.pop(WARM_UP_EXIT_TEXT) or "").strip()
            if bridge:
                reason = acknowledgement_rejection_reason(
                    bridge, recent=ctx.recent_lines, answer=ctx.candidate_text
                ) or ("announces_process" if announces_process(bridge) else None)
                if reason:
                    notes.append("transition_text_rejected")
                    trace_event(
                        "interviewer", "unified_text_rejected", action=name, argument=WARM_UP_EXIT_TEXT,
                        reason=reason, text=bridge[:160], session_id=str(ctx.session_id),
                    )
                else:
                    args[WARM_UP_EXIT_TEXT] = bridge
        # Unified Thinker (§6.2): the utterance written in the deciding call.
        # Each action's text is checked by that action's own guard, never a
        # shared one -- a probe must be a question, a clarification must not
        # lecture, an answer to the candidate must stay inside the posting. A
        # rejected text is dropped rather than failing the turn: the decision
        # stands and the handler writes the line as it always did, which costs
        # the second call back but never costs the candidate a turn.
        argument = UNIFIED_TEXT_ACTIONS.get(name)
        if argument and argument in args:
            kept = _checked_unified_text(ctx, action=name, argument=argument, raw=args.get(argument))
            if kept:
                args[argument] = kept
            else:
                args.pop(argument, None)
                notes.append(f"{argument}_rejected")
        # The evaluation is advisory context for the record, never a score.
        evaluation = args.pop("evaluation", None)
        if isinstance(evaluation, dict):
            args["evaluation"] = {
                "covered": [str(x) for x in (evaluation.get("covered") or [])],
                "missing": [str(x) for x in (evaluation.get("missing") or [])],
            }
        decision = Decision(
            name,
            args,
            reason=reason,
            evidence_refs=[str(r) for r in (refs or [])],
            source="agent",
            path=["model"] + notes,
        )
        if memory_hits:
            # Distinct items, not round trips: once memory is prefetched, a
            # ``search_candidate_memory`` for the same skill returns what the
            # model was already shown, and counting it twice would overstate
            # how much the decision drew on.
            seen = {(str(h.get("skill_key") or ""), str(h.get("text") or "")) for h in memory_hits}
            decision.args["memory_hits"] = len(seen)
            decision.args["memory_prefetched"] = len(prefetched)
        return decision
    return _violation("memory_loop", f"{max_steps} steps")


def is_violation(decision: Decision) -> Optional[str]:
    return decision.args.get("violation") if decision.source == "agent" else None
