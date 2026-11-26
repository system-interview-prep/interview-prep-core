"""T2.2.6–T2.2.8, T3.2.1 (shadow) and T2.3.1–T2.3.2 (live actions flag)."""

from __future__ import annotations

import copy

import pytest

from src.modules.interviews.agents.interviewer import thinker as decide_module
from src.modules.interviews.agents.interviewer import graph as turn_graph
from src.modules.interviews.core.contracts.interview import CandidateTurnInput, TurnAction
from src.modules.interviews.agents.interviewer.engine import InterviewCoreEngine
from tests.modules.interviews.agent.conftest import FakeToolCaller, tool_call
from tests.modules.interviews.agent.test_turn_graph import ScriptedLLM

INSUFFICIENT = {"intent": "ANSWER", "sufficiency_status": "INSUFFICIENT", "missing_aspect": "xử lý khi Redis lỗi",
                "acknowledgement": "Ừ, mình hình dung được rồi."}
ANSWER = "Em dùng Redis làm cache với TTL năm phút và xoá key khi dữ liệu đổi để tránh đọc cũ."


def _turn(text: str = ANSWER) -> CandidateTurnInput:
    return CandidateTurnInput(session_id="sess-shadow", turn_index=3, text_content=text)


@pytest.fixture
def scripted_model(monkeypatch):
    """Route the model decider to a FakeToolCaller for the test."""

    def install(*answers):
        caller = FakeToolCaller(*answers)
        monkeypatch.setattr(decide_module, "generate_tool_calls", caller)
        return caller

    return install


@pytest.mark.asyncio
async def test_t2_2_6_shadow_records_the_disagreement_but_executes_the_rule(session_state_factory, scripted_model) -> None:
    caller = scripted_model([tool_call("move_to_next_question", reason="pace_behind")])
    state = session_state_factory()
    rule_out = await InterviewCoreEngine(llm_client=ScriptedLLM(INSUFFICIENT)).handle_turn(_turn(), copy.deepcopy(state))
    out = await turn_graph.run_turn(InterviewCoreEngine(llm_client=ScriptedLLM(INSUFFICIENT)), _turn(), state, mode="shadow")
    rec = out.metadata["decision"]
    assert out.action == rule_out.action == TurnAction.PROBE and out.message_text == rule_out.message_text
    assert rec["executed"] == "rule" and rec["rule"]["action"] == "acknowledge_and_probe"
    assert rec["agent"]["action"] == "move_to_next_question" and rec["agreement"] is False
    assert len(caller.inputs) == 1


@pytest.mark.asyncio
async def test_t2_2_7_a_failing_decider_costs_nothing(session_state_factory, scripted_model) -> None:
    scripted_model(TimeoutError("provider slow"))
    out = await turn_graph.run_turn(InterviewCoreEngine(llm_client=ScriptedLLM(INSUFFICIENT)), _turn(), session_state_factory(), mode="shadow")
    rec = out.metadata["decision"]
    assert out.action == TurnAction.PROBE
    assert rec["decide_error"] == "TimeoutError" and rec["agent"] is None and rec["agreement"] is None


@pytest.mark.asyncio
async def test_t2_2_8_shadow_adds_exactly_one_model_call(session_state_factory, scripted_model) -> None:
    caller = scripted_model([tool_call("acknowledge_and_probe", kind="gap", missing_aspect="x", rationale="gap")])
    llm = ScriptedLLM(INSUFFICIENT)
    await turn_graph.run_turn(InterviewCoreEngine(llm_client=llm), _turn(), session_state_factory(), mode="shadow")
    assert llm.calls == ["json", "text"]  # evaluate + probe, as the rule path
    assert len(caller.inputs) == 1  # + decide


@pytest.mark.asyncio
async def test_t3_2_1_shadow_never_changes_the_output_whatever_the_model_picks(session_state_factory, scripted_model) -> None:
    cases = [
        ("Bạn nói lại câu hỏi giúp em được không?", {}, "clarify_question"),
        ("Em xin đổi câu khác được không?", {}, "acknowledge_and_probe"),
        (ANSWER, INSUFFICIENT, "move_to_next_question"),
        (ANSWER, {"intent": "ANSWER", "sufficiency_status": "SUFFICIENT", "acknowledgement": "Ừ, rõ rồi."}, "clarify_question"),
        ("Em không biết ạ.", {"intent": "GIVE_UP", "sufficiency_status": "INSUFFICIENT", "acknowledgement": "Không sao."}, "repeat_question"),
    ]
    for text, evaluation, other in cases:
        scripted_model([tool_call(other, rationale="different on purpose")])
        state = session_state_factory()
        rule_out = await InterviewCoreEngine(llm_client=ScriptedLLM(evaluation or None)).handle_turn(_turn(text), copy.deepcopy(state))
        out = await turn_graph.run_turn(InterviewCoreEngine(llm_client=ScriptedLLM(evaluation or None)), _turn(text), state, mode="shadow")
        assert (out.action, out.current_stage, out.message_text) == (rule_out.action, rule_out.current_stage, rule_out.message_text), text
        assert out.metadata["decision"]["executed"] == "rule"


@pytest.mark.asyncio
async def test_t2_3_1_live_executes_an_in_menu_action(session_state_factory, scripted_model) -> None:
    scripted_model([tool_call("clarify_question", rationale="the candidate is confused")])
    llm = ScriptedLLM(INSUFFICIENT, line="Ý mình là bạn xử lý thế nào khi cache không còn đúng?")
    out = await turn_graph.run_turn(InterviewCoreEngine(llm_client=llm), _turn(), session_state_factory(), mode="live")
    rec = out.metadata["decision"]
    assert rec["executed"] == "agent" and out.action == TurnAction.CLARIFY
    assert out.metadata["clarify_used"] is True


@pytest.mark.asyncio
async def test_t2_3_2_live_executes_any_in_menu_action_without_an_action_allowlist(session_state_factory, scripted_model) -> None:
    scripted_model([tool_call("move_to_next_question", reason="budget")])
    out = await turn_graph.run_turn(InterviewCoreEngine(llm_client=ScriptedLLM(INSUFFICIENT)), _turn(), session_state_factory(), mode="live")
    rec = out.metadata["decision"]
    assert rec["executed"] == "agent"
    assert rec["agent"]["action"] == "move_to_next_question"


@pytest.mark.asyncio
async def test_t2_4_1_live_uses_the_agent_choice_without_a_per_action_flag(session_state_factory, scripted_model) -> None:
    scripted_model([tool_call("move_to_next_question", reason="sufficient")])
    out = await turn_graph.run_turn(InterviewCoreEngine(llm_client=ScriptedLLM(INSUFFICIENT)), _turn(), session_state_factory(), mode="live")
    rec = out.metadata["decision"]
    assert rec["executed"] == "agent"
    assert rec["agent"]["action"] == "move_to_next_question"


def test_agent_mode_reads_the_environment(monkeypatch) -> None:
    for raw, expected in (("", "rule"), ("rule", "rule"), ("shadow", "shadow"), ("live", "live"), ("bogus", "rule")):
        monkeypatch.setenv("INTERVIEW_AGENT_MODE", raw)
        assert turn_graph.agent_mode() == expected
