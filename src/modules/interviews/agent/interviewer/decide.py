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

from src.modules.ai.facade import generate_tool_calls
from src.modules.interviews.agent.interviewer.tools import SEARCH_MEMORY, tools_for
from src.modules.interviews.core.guardrails.output import (
    WARMUP_PROBE_MAX_QUESTIONS,
    WARMUP_PROBE_MAX_SENTENCES,
    WARMUP_PROBE_MAX_WORDS,
    checked_probe,
)
from src.modules.interviews.core.conversation.prompt.decide import decide_input, decide_prompt
from src.modules.interviews.core.conversation.turn_context import Decision, TurnContext

PURPOSE = "interviews.agent.decide"
MemorySearch = Callable[[str], Awaitable[Sequence[Dict[str, Any]]]]
ToolCaller = Callable[..., Awaitable[List[Dict[str, Any]]]]


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
) -> Decision:
    # Resolved at call time so a test can route the decider through a scripted caller.
    generate = generate or generate_tool_calls
    tools = tools_for(allowed_actions)
    memory_hits: List[Dict[str, Any]] = []
    notes: List[str] = []
    items: Any = decide_input(
        question=ctx.active_prompt or ctx.question_prompt,
        candidate_text=ctx.candidate_text,
        stage=ctx.current_stage.value,
        evaluation=ctx.eval_result or None,
        assessment_criteria=str(ctx.current_question.get("rubric_criteria") or ""),
        recent_lines=ctx.recent_lines,
        pacing=pacing,
        allowed_actions=allowed_actions,
        interview_contract=ctx.session_state.get("interview_contract"),
        is_vi=ctx.is_vi,
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
                max_words=WARMUP_PROBE_MAX_WORDS,
                max_sentences=WARMUP_PROBE_MAX_SENTENCES,
                max_questions=WARMUP_PROBE_MAX_QUESTIONS,
            )
            if not follow_up:
                return _violation("invalid_follow_up", "missing or rejected warm-up utterance", calls)
            args["follow_up_text"] = follow_up
        decision = Decision(
            name,
            args,
            reason=reason,
            evidence_refs=[str(r) for r in (refs or [])],
            source="agent",
            path=["model"] + notes,
        )
        if memory_hits:
            decision.args["memory_hits"] = len(memory_hits)
        return decision
    return _violation("memory_loop", f"{max_steps} steps")


def is_violation(decision: Decision) -> Optional[str]:
    return decision.args.get("violation") if decision.source == "agent" else None
