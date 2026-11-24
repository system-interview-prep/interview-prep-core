"""T1.2.1–T1.2.3 and T2.2.1–T2.2.5 — the model decider and its guard rails."""

from __future__ import annotations

import re

import pytest

from src.modules.interviews.agent.interviewer import decide as decide_module
from src.modules.interviews.agent.interviewer.decide import decide_by_model, is_violation
from src.modules.interviews.agent.interviewer.tools import SEARCH_MEMORY, TOOLS, tools_for
from src.modules.interviews.core.conversation.prompt.decide import decide_input, decide_prompt
from src.modules.interviews.core.conversation.turn_context import ACTIONS
from src.modules.interviews.core.conversation.turn_engine import InterviewCoreEngine
from src.modules.interviews.core.conversation.turn_rules import PROHIBITED_LEAK_PATTERNS
from src.modules.interviews.core.contracts.interview import InterviewStage
from tests.modules.interviews.agent.conftest import FakeToolCaller, tool_call

MENU = ["confirm_abort", "repeat_question", "answer_candidate_question", "decline_system_probe",
        "redirect_off_topic", "clarify_question", "acknowledge_and_probe", "move_to_next_question"]
PACING = {"pace": "on_track", "questions_remaining_by_stage": {"DEEP_DIVE": 1}}


def test_t1_2_1_every_action_has_one_strict_tool_and_one_act(ctx_factory) -> None:
    assert set(TOOLS) == set(ACTIONS) | {SEARCH_MEMORY}
    for name, tool in TOOLS.items():
        assert tool["strict"] is True and tool["parameters"]["additionalProperties"] is False
        assert {"rationale", "evidence_refs"} <= set(tool["parameters"]["properties"])
        assert set(tool["parameters"]["required"]) == set(tool["parameters"]["properties"])
        if name != SEARCH_MEMORY:
            assert hasattr(InterviewCoreEngine, f"act_{name}"), name
    assert [t["name"] for t in tools_for(["clarify_question", "nope", "confirm_abort"])] == ["clarify_question", "confirm_abort"]


@pytest.mark.parametrize("is_vi", [True, False])
def test_t1_2_2_the_decide_prompt_leaks_no_scoring_words(is_vi: bool) -> None:
    prompt = decide_prompt(is_vi)
    for pattern in PROHIBITED_LEAK_PATTERNS:
        assert not re.search(pattern, prompt, re.IGNORECASE), (pattern, prompt)


@pytest.mark.parametrize("is_vi", [True, False])
def test_decide_prompt_distinguishes_repeat_from_incomplete_answer(is_vi: bool) -> None:
    prompt = decide_prompt(is_vi).casefold()
    assert "repeat_question" in prompt
    assert "continue_warm_up" in prompt
    if is_vi:
        assert "không nghe được" in prompt
        assert "câu trả lời ngắn" in prompt
        assert "không lặp câu mở đầu" in prompt
    else:
        assert "could not hear the question" in prompt
        assert "answer is short" in prompt
        assert "do not repeat the original opening question" in prompt


@pytest.mark.parametrize("is_vi", [True, False])
def test_warmup_decide_prompt_caps_followup_length(is_vi: bool) -> None:
    prompt = decide_prompt(is_vi).casefold()
    assert "32" in prompt
    if not is_vi:
        assert "at most 32 words" in prompt


def test_action_tool_descriptions_reinforce_repeat_and_warmup_guidance() -> None:
    repeat = TOOLS["repeat_question"]["description"].casefold()
    warmup = TOOLS["continue_warm_up"]["description"].casefold()
    assert "chỉ dùng khi ứng viên nói rõ" in repeat
    assert "không dùng cho câu trả lời ngắn" in repeat
    assert "không lặp câu mở đầu" in warmup
    schema = TOOLS["continue_warm_up"]["parameters"]
    assert "follow_up_text" in schema["required"]
    assert schema["properties"]["follow_up_text"]["maxLength"] == 280


def test_t1_2_3_the_decide_input_carries_no_rubric_score_or_other_session(ctx_factory) -> None:
    text = decide_input(
        question="Bạn dùng Redis cache thế nào?",
        candidate_text="Em đặt TTL năm phút.",
        stage="DEEP_DIVE",
        evaluation={"intent": "ANSWER", "sufficiency_status": "INSUFFICIENT", "missing_aspect": "xử lý lỗi",
                    "score": 4.5, "rubric_criteria": "điểm chính: invalidation"},
        recent_lines=["Ừm, mình hiểu rồi."],
        pacing=PACING,
        allowed_actions=MENU,
        memory_hits=[{"text": "Còn hở phần invalidation", "evidence": {"quote": "lần trước em nói…"}}],
    )
    assert "4.5" not in text and "rubric" not in text.lower() and "điểm chính" not in text
    assert "lần trước em nói" not in text  # memory contributes its topic, never its evidence
    assert "<candidate_response>" in text and "Menu: " in text and "pace" in text


@pytest.mark.asyncio
async def test_live_warmup_action_returns_agent_authored_line_without_recovery(ctx_factory, monkeypatch) -> None:
    prior_question = "Bạn giới thiệu ngắn về bản thân và lý do quan tâm vị trí này?"
    agent_line = "Em nghe bạn yêu thích C#. Điều gì ở vị trí này khiến bạn muốn ứng tuyển?"
    ctx = ctx_factory(
        text="Em đam mê lập trình C#.",
        stage=InterviewStage.WARM_UP,
        extra={"recent_interviewer_lines": [prior_question]},
    )
    caller = FakeToolCaller([
        tool_call("continue_warm_up", missing_aspect="lý do quan tâm vị trí", follow_up_text=agent_line)
    ])
    decision = await decide_by_model(
        ctx,
        allowed_actions=["continue_warm_up"],
        pacing=PACING,
        generate=caller,
    )

    assert decision.action == "continue_warm_up"
    assert decision.args["follow_up_text"] == agent_line
    model_input = caller.inputs[0]["input_items"]
    assert prior_question in model_input
    assert "Em đam mê lập trình C#" in model_input

    engine = InterviewCoreEngine()

    async def unexpected_recovery(**_kwargs):
        raise AssertionError("warm-up live action must not call a recovery LLM")

    monkeypatch.setattr(engine, "_generate_interviewer_line", unexpected_recovery)
    output = await engine.act_continue_warm_up(ctx, decision)
    assert output.message_text == agent_line


@pytest.mark.asyncio
async def test_t2_2_1_a_tool_outside_the_menu_is_a_violation_not_an_exception(ctx_factory) -> None:
    caller = FakeToolCaller([tool_call("close_interview", reason="complete", rationale="done")])
    d = await decide_by_model(ctx_factory(), allowed_actions=MENU, pacing=PACING, generate=caller)
    assert is_violation(d) == "outside_menu" and d.source == "agent"
    assert [t["name"] for t in caller.inputs[0]["tools"]] == MENU  # the model only ever saw the menu


@pytest.mark.asyncio
async def test_t2_2_2_two_calls_take_the_first_and_note_it(ctx_factory) -> None:
    caller = FakeToolCaller([tool_call("clarify_question", rationale="unclear"), tool_call("move_to_next_question", reason="budget")])
    d = await decide_by_model(ctx_factory(), allowed_actions=MENU, pacing=PACING, generate=caller)
    assert d.action == "clarify_question" and "multi_call" in d.path and is_violation(d) is None


@pytest.mark.asyncio
async def test_t2_2_3_bad_json_or_missing_reason_falls_back(ctx_factory) -> None:
    for answer in ([{"name": "clarify_question", "arguments": {"_raw": "{not json"}, "call_id": "c"}],
                   [{"name": "clarify_question", "arguments": {"evidence_refs": []}, "call_id": "c"}],
                   []):
        d = await decide_by_model(ctx_factory(), allowed_actions=MENU, pacing=PACING, generate=FakeToolCaller(answer))
        assert is_violation(d) == "parse", answer


@pytest.mark.asyncio
async def test_t2_2_4_memory_search_then_action_is_two_calls(ctx_factory) -> None:
    async def memory(skill_key: str):
        return [{"kind": "gap", "skill_key": skill_key, "text": "Còn hở phần invalidation"}]

    caller = FakeToolCaller(
        [tool_call(SEARCH_MEMORY, call_id="m1", skill_key="skill-redis")],
        [tool_call("acknowledge_and_probe", kind="gap", missing_aspect="invalidation", rationale="gap seen before")],
    )
    d = await decide_by_model(ctx_factory(), allowed_actions=MENU + [SEARCH_MEMORY], pacing=PACING,
                              generate=caller, memory_search=memory)
    assert d.action == "acknowledge_and_probe" and d.args["memory_hits"] == 1
    assert len(caller.inputs) == 2
    second = caller.inputs[1]["input_items"]
    assert isinstance(second, list) and second[-1]["type"] == "function_call_output"
    assert "invalidation" in second[-1]["output"]


@pytest.mark.asyncio
async def test_t2_2_5_memory_loop_stops_at_max_steps(ctx_factory) -> None:
    async def memory(skill_key: str):
        return []

    caller = FakeToolCaller(*[[tool_call(SEARCH_MEMORY, call_id=f"m{i}", skill_key="x")] for i in range(3)])
    d = await decide_by_model(ctx_factory(), allowed_actions=MENU + [SEARCH_MEMORY], pacing=PACING,
                              generate=caller, memory_search=memory, max_steps=2)
    assert is_violation(d) == "memory_loop" and len(caller.inputs) == 2


@pytest.mark.asyncio
async def test_memory_search_without_the_menu_entry_is_outside_menu(ctx_factory) -> None:
    caller = FakeToolCaller([tool_call(SEARCH_MEMORY, skill_key="x")])
    d = await decide_by_model(ctx_factory(), allowed_actions=MENU, pacing=PACING, generate=caller)
    assert is_violation(d) == "outside_menu"


@pytest.mark.asyncio
async def test_the_decider_goes_through_the_facade_by_default(ctx_factory, monkeypatch) -> None:
    seen = {}

    async def fake(**kwargs):
        seen.update(kwargs)
        return [tool_call("move_to_next_question", reason="sufficient", rationale="all points covered")]

    monkeypatch.setattr(decide_module, "generate_tool_calls", fake)
    d = await decide_by_model(ctx_factory(), allowed_actions=MENU, pacing=PACING)
    assert d.action == "move_to_next_question"
    assert seen["purpose"] == "interviews.agent.decide" and seen["tool_choice"] == "required"
    assert seen["reasoning_effort"] == "none"
