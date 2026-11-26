"""The warm-up knows when it is done (session e52c0757, 2026-11-26, part two).

Bounding the follow-ups stopped the loop; this is what lets the interviewer
*know* the introduction is complete instead of hitting the cap: it sees the
exchange as pairs (what it asked, what the candidate replied) with the budget,
a follow-up that asks an earlier ask again ends the warm-up, and leaving the
warm-up carries one model-written bridging sentence so the first real question
does not land cold.
"""

from __future__ import annotations

import pytest

from src.modules.interviews.agents.interviewer import graph as interview_agent
from src.modules.interviews.agents.interviewer.engine import InterviewCoreEngine
from src.modules.interviews.agents.interviewer.prompts.decide import decide_input, warm_up_exchange_block
from src.modules.interviews.agents.interviewer.thinker import decide_by_model, is_violation
from src.modules.interviews.agents.interviewer.tools import TOOLS, tools_for
from src.modules.interviews.core.contracts.interview import CandidateTurnInput, InterviewStage, TurnAction
from src.modules.interviews.core.conversation.turn_context import Decision
from src.modules.interviews.core.conversation.turn_rules import warm_up_follow_up_repeats, warm_up_follow_ups_exhausted
from tests.modules.interviews.agent.conftest import FakeToolCaller, tool_call
from tests.modules.interviews.agent.test_call_budget import CountingLLM

# The real lines of session e52c0757, lightly shortened.
OPENING = "Bạn giới thiệu ngắn về bản thân trong một, hai phút: bạn đang làm gì và vì sao quan tâm vị trí này?"
ASKED_REASON = (
    "Cảm ơn bạn đã chia sẻ. Bạn có thể cho mình biết thêm về lý do bạn quan tâm đến vị trí lập trình viên này không? "
    "Điều gì ở vị trí này làm bạn hứng thú?"
)
REASON_AGAIN = (
    "Cảm ơn bạn đã chia sẻ về trải nghiệm học tập và dự án của mình. Bạn có thể cho mình biết thêm về lý do cụ thể "
    "bạn quan tâm đến vị trí lập trình viên này? Có điều gì ở vị trí này làm bạn thấy hứng thú không?"
)
INTRO = "Dạ hiện tại em là sinh viên năm cuối ngành kỹ thuật phần mềm."
REASON = (
    "Em có định hướng phát triển trong lĩnh vực AI và backend, em quan tâm tới vị trí này vì mong muốn được áp dụng "
    "những kiến thức đã học vào môi trường doanh nghiệp."
)
PACING = {"pace": "on_track", "questions_remaining_by_stage": {"VALIDATE": 1}}
MENU = ["continue_warm_up", "move_to_next_question"]


def test_the_same_ask_in_other_words_is_a_repeat_and_a_new_ask_is_not() -> None:
    assert warm_up_follow_up_repeats(REASON_AGAIN, [OPENING, ASKED_REASON]) == "follow_up:2"
    assert warm_up_follow_up_repeats(
        "Bạn cho mình hỏi thêm về lý do bạn quan tâm đến vị trí này là gì?", [ASKED_REASON]
    ) == "follow_up:1"
    assert warm_up_follow_up_repeats("Bạn đang học năm mấy vậy?", [OPENING, ASKED_REASON]) is None
    assert warm_up_follow_up_repeats("Bạn làm công việc đó được bao lâu rồi?", [ASKED_REASON]) is None
    assert warm_up_follow_up_repeats(ASKED_REASON, []) is None


def test_a_repeated_follow_up_ends_the_warm_up(ctx_factory) -> None:
    ctx = ctx_factory(stage=InterviewStage.WARM_UP, clarify_used=1, elapsed_ratio=0.05)
    assert warm_up_follow_ups_exhausted(ctx) is None
    ctx.warm_up_repeat = "follow_up:1"
    assert warm_up_follow_ups_exhausted(ctx) == "repeated_follow_up:follow_up:1"


def test_the_decider_sees_the_exchange_as_pairs_with_the_budget(ctx_factory) -> None:
    ctx = ctx_factory(
        text=f"{INTRO}\n{REASON}",
        stage=InterviewStage.WARM_UP,
        clarify_used=1,
        extra={"warm_up_follow_ups": [ASKED_REASON], "recent_interviewer_lines": [ASKED_REASON]},
    )
    assert ctx.warm_up_follow_ups == [ASKED_REASON]
    block = warm_up_exchange_block(
        {"follow_ups": ctx.warm_up_follow_ups, "fragments": [INTRO, REASON], "used": 1, "limit": 2}
    )
    lines = block.split("\n")
    assert lines[0] == "Diễn biến phần mở đầu (bạn đã hỏi thêm 1/2 lần):"
    # Fragment 0 answers the opening; follow-up 1 then fragment 1 answers it.
    assert lines.index(f"Bạn hỏi thêm 1: {ASKED_REASON}") > lines.index("Ứng viên đáp: <candidate_response>")
    assert block.count("<candidate_response>") == 2 and REASON in block
    text = decide_input(
        question=OPENING, candidate_text=ctx.candidate_text, stage="WARM_UP", evaluation=None,
        recent_lines=ctx.recent_lines, pacing=PACING, allowed_actions=MENU,
        warm_up_exchange={"follow_ups": [ASKED_REASON], "fragments": [INTRO, REASON], "used": 1, "limit": 2},
    )
    assert "Diễn biến phần mở đầu" in text and "Các câu bạn vừa nói" in text
    assert "Diễn biến" not in decide_input(
        question="Q", candidate_text="A", stage="DEEP_DIVE", evaluation=None,
        recent_lines=[], pacing=PACING, allowed_actions=MENU,
    )


def test_the_prompt_defines_enough_and_asks_each_point_once() -> None:
    from src.modules.interviews.agents.interviewer.prompts.decide import decide_prompt
    from src.modules.interviews.agents.interviewer.prompts.evaluation import WARM_UP_GUIDE_VI

    for text in (decide_prompt(True), TOOLS["continue_warm_up"]["description"], WARM_UP_GUIDE_VI):
        assert "một lần" in text
    for text in (decide_prompt(True), WARM_UP_GUIDE_VI):
        assert "cụ thể hơn" in text
    assert "transition_text" in decide_prompt(True) and "transition_text" in decide_prompt(False)


def test_the_exit_tool_carries_a_bridge_when_asked() -> None:
    warm = tools_for(MENU, bridge=True)
    assert [t["name"] for t in warm] == MENU
    assert "transition_text" in warm[1]["parameters"]["properties"]
    assert "transition_text" not in tools_for(MENU)[1]["parameters"]["properties"]
    assert "transition_text" not in tools_for(MENU, unified=True)[1]["parameters"]["properties"]


@pytest.mark.asyncio
async def test_the_model_asking_the_same_thing_again_is_rejected_and_the_turn_moves_on(ctx_factory) -> None:
    ctx = ctx_factory(
        text=f"{INTRO}\n{REASON}", stage=InterviewStage.WARM_UP, clarify_used=1,
        extra={"warm_up_follow_ups": [ASKED_REASON], "recent_interviewer_lines": [ASKED_REASON]},
    )
    caller = FakeToolCaller([tool_call("continue_warm_up", missing_aspect="lý do", follow_up_text=REASON_AGAIN)])
    decision = await decide_by_model(ctx, allowed_actions=MENU, pacing=PACING, generate=caller)
    assert is_violation(decision) == "repeated_follow_up"
    assert ctx.warm_up_repeat == "follow_up:1"
    # The rule ladder now sees the warm-up as finished.
    assert warm_up_follow_ups_exhausted(ctx).startswith("repeated_follow_up")


@pytest.mark.asyncio
async def test_a_fresh_follow_up_still_passes(ctx_factory) -> None:
    ctx = ctx_factory(
        text=INTRO, stage=InterviewStage.WARM_UP, clarify_used=0,
        extra={"warm_up_follow_ups": [], "recent_interviewer_lines": [OPENING]},
    )
    line = "Năm cuối rồi, chắc đang bận đồ án nhỉ. Điều gì ở vị trí này khiến bạn muốn ứng tuyển?"
    caller = FakeToolCaller([tool_call("continue_warm_up", missing_aspect="lý do", follow_up_text=line)])
    decision = await decide_by_model(ctx, allowed_actions=MENU, pacing=PACING, generate=caller)
    assert is_violation(decision) is None and decision.args["follow_up_text"] == line
    assert "Diễn biến phần mở đầu (bạn đã hỏi thêm 0/2 lần)" in caller.inputs[0]["input_items"]


@pytest.mark.asyncio
async def test_leaving_the_warm_up_speaks_the_bridge_then_the_frozen_question(ctx_factory) -> None:
    ctx = ctx_factory(text=f"{INTRO}\n{REASON}", stage=InterviewStage.WARM_UP, clarify_used=1)
    bridge = "Mình hiểu rồi, giờ mình hỏi về dự án trong CV của bạn nhé."
    caller = FakeToolCaller([tool_call("move_to_next_question", reason="sufficient", transition_text=bridge)])
    decision = await decide_by_model(ctx, allowed_actions=MENU, pacing=PACING, generate=caller)
    assert is_violation(decision) is None and decision.args["transition_text"] == bridge

    output = await InterviewCoreEngine().act_move_to_next_question(ctx, decision)
    assert output.action == TurnAction.NEXT_QUESTION
    head, _, question = output.message_text.partition("\n\n")
    assert head == bridge and question
    assert output.metadata["transition"] == bridge
    assert [step["act"] for step in output.metadata["speech_plan"]] == ["TRANSITION", "QUESTION"]


@pytest.mark.asyncio
async def test_a_bridge_that_asks_or_grades_is_dropped_and_the_question_stays(ctx_factory) -> None:
    ctx = ctx_factory(text=INTRO, stage=InterviewStage.WARM_UP, clarify_used=1)
    for bad in ("Bạn còn muốn bổ sung gì không?", "Phần giới thiệu của bạn rất tốt và đầy đủ."):
        caller = FakeToolCaller([tool_call("move_to_next_question", reason="sufficient", transition_text=bad)])
        decision = await decide_by_model(ctx, allowed_actions=MENU, pacing=PACING, generate=caller)
        assert is_violation(decision) is None
        assert "transition_text" not in decision.args and "transition_text_rejected" in decision.path
        output = await InterviewCoreEngine().act_move_to_next_question(ctx, decision)
        assert "\n\n" not in output.message_text and output.metadata["transition"] == ""

    # The handler guards it too: a bridge that slipped past the decider is checked again.
    sneaky = Decision("move_to_next_question", {"reason": "sufficient", "transition_text": "Bạn học năm mấy?"}, source="agent")
    output = await InterviewCoreEngine().act_move_to_next_question(ctx, sneaky)
    assert "\n\n" not in output.message_text and output.metadata["transition"] == ""


@pytest.mark.asyncio
async def test_a_bridge_that_narrates_the_process_is_dropped_in_any_stage(ctx_factory) -> None:
    """Since session 0e3c14c6 every move may carry a bridge; this one narrates the process."""
    ctx = ctx_factory(stage=InterviewStage.DEEP_DIVE)
    decision = Decision("move_to_next_question", {"reason": "sufficient", "transition_text": "Mình sang chủ đề khác nhé."}, source="agent")
    output = await InterviewCoreEngine().act_move_to_next_question(ctx, decision)
    assert output.metadata["transition"] == "" and "\n\n" not in output.message_text


@pytest.mark.asyncio
async def test_live_turn_the_session_would_have_had(session_state_factory, monkeypatch) -> None:
    """Replay of 15:01:36: the reason was already given and already asked for;
    the model asks for it once more; the rule ends the warm-up instead."""
    async def asks_again(ctx, *, allowed_actions, **_kw):
        return await decide_by_model(
            ctx, allowed_actions=allowed_actions, pacing=PACING,
            generate=FakeToolCaller([tool_call("continue_warm_up", missing_aspect="lý do", follow_up_text=REASON_AGAIN)]),
        )

    monkeypatch.setattr(interview_agent, "decide_by_model", asks_again)
    llm = CountingLLM(evaluation={"intent": "VALID_ANSWER", "sufficiency_status": "INSUFFICIENT",
                                  "missing_aspect": "lý do", "acknowledgement": "Cảm ơn bạn.",
                                  "probe_question": REASON_AGAIN, "score": 4.0})
    state = session_state_factory(
        stage=InterviewStage.WARM_UP, clarify_used=1, elapsed_ratio=0.05,
        extra={"warm_up_follow_ups": [ASKED_REASON], "recent_interviewer_lines": [ASKED_REASON]},
    )
    output = await interview_agent.run_turn(
        InterviewCoreEngine(llm_client=llm),
        CandidateTurnInput(session_id="sess-e52c", turn_index=0, text_content=f"{INTRO}\n{REASON}"),
        state, mode=interview_agent.MODE_LIVE,
    )
    assert output.action == TurnAction.NEXT_QUESTION
    assert output.metadata["decision"]["executed"] == "rule"
    assert output.metadata["decision"]["transition"]["validation"] == "rejected:repeated_follow_up"
