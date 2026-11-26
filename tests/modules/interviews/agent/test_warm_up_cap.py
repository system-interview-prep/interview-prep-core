"""The warm-up cannot repeat its follow-up forever (session e52c0757, 2026-11-26).

That session stayed on turn 0 for six and a half minutes: twelve
``continue_warm_up`` decisions in a row, every one asking for "lý do quan tâm
vị trí", while the menu offered ``move_to_next_question`` each time and the
pacing block said the warm-up share was spent from 0.10 on. Nothing
deterministic bounded it. Now the blueprint's ``warm_up.max_follow_ups``
(default 2) and the stage's time share do, for the menu and the rule ladder
alike. The model still writes the follow-ups; it can no longer ask forever.
"""

from __future__ import annotations

import pytest

from src.modules.interviews.agents.interviewer import graph as interview_agent
from src.modules.interviews.agents.interviewer.engine import InterviewCoreEngine
from src.modules.interviews.agents.interviewer.menu import build_menu
from src.modules.interviews.core.contracts.interview import CandidateTurnInput, InterviewStage, TurnAction
from src.modules.interviews.core.conversation.turn_rules import warm_up_follow_ups_exhausted
from src.modules.interviews.core.session_policy.blueprint import default_blueprint
from tests.modules.interviews.agent.test_call_budget import CountingLLM


def test_the_blueprint_carries_the_cap_with_a_default_of_two() -> None:
    policy = default_blueprint()
    assert policy.warm_up.max_follow_ups == 2
    assert policy.to_payload()["warmUp"]["maxFollowUps"] == 2


def test_follow_ups_are_bounded_by_count_and_by_time(ctx_factory) -> None:
    assert warm_up_follow_ups_exhausted(ctx_factory(stage=InterviewStage.WARM_UP, clarify_used=0, elapsed_ratio=0.05)) is None
    assert warm_up_follow_ups_exhausted(ctx_factory(stage=InterviewStage.WARM_UP, clarify_used=1, elapsed_ratio=0.05)) is None
    assert warm_up_follow_ups_exhausted(ctx_factory(stage=InterviewStage.WARM_UP, clarify_used=2, elapsed_ratio=0.05)) == "follow_ups:2/2"
    # A long-winded introduction gets twice the stage's share, not more.
    assert warm_up_follow_ups_exhausted(ctx_factory(stage=InterviewStage.WARM_UP, clarify_used=0, elapsed_ratio=0.19)) is None
    assert warm_up_follow_ups_exhausted(ctx_factory(stage=InterviewStage.WARM_UP, clarify_used=0, elapsed_ratio=0.29)).startswith("time:")
    # Other stages have their own rules.
    assert warm_up_follow_ups_exhausted(ctx_factory(stage=InterviewStage.DEEP_DIVE, clarify_used=9, elapsed_ratio=0.9)) is None


def test_the_menu_withdraws_the_continuation_once_the_cap_is_reached(ctx_factory) -> None:
    open_menu = build_menu(ctx_factory(stage=InterviewStage.WARM_UP, clarify_used=1, elapsed_ratio=0.05))
    assert "continue_warm_up" in open_menu.allowed
    capped = build_menu(ctx_factory(stage=InterviewStage.WARM_UP, clarify_used=2, elapsed_ratio=0.05))
    assert "continue_warm_up" not in capped.allowed
    assert "move_to_next_question" in capped.allowed
    assert capped.reasons["continue_warm_up"] == "warm-up follow-ups exhausted (follow_ups:2/2)"
    late = build_menu(ctx_factory(stage=InterviewStage.WARM_UP, clarify_used=0, elapsed_ratio=0.41))
    assert "continue_warm_up" not in late.allowed and "move_to_next_question" in late.allowed


@pytest.mark.asyncio
async def test_the_rule_ladder_moves_on_after_the_cap_even_when_the_answer_is_still_thin(session_state_factory) -> None:
    llm = CountingLLM(evaluation={
        "intent": "VALID_ANSWER", "sufficiency_status": "INSUFFICIENT",
        "missing_aspect": "lý do quan tâm vị trí", "acknowledgement": "Cảm ơn bạn.",
        "probe_question": "Bạn có thể nói thêm về lý do bạn quan tâm đến vị trí này không?", "score": 4.0,
    })
    engine = InterviewCoreEngine(llm_client=llm)
    turn = CandidateTurnInput(session_id="sess-warm", turn_index=0, text_content="Dạ em là sinh viên năm cuối ngành kỹ thuật phần mềm.")

    still_open = await interview_agent.run_turn(
        engine, turn, session_state_factory(stage=InterviewStage.WARM_UP, clarify_used=1, elapsed_ratio=0.05),
        mode=interview_agent.MODE_RULE,
    )
    assert still_open.action == TurnAction.CLARIFY, "one follow-up left: ask it"

    capped = await interview_agent.run_turn(
        engine, turn, session_state_factory(stage=InterviewStage.WARM_UP, clarify_used=2, elapsed_ratio=0.05),
        mode=interview_agent.MODE_RULE,
    )
    assert capped.action == TurnAction.NEXT_QUESTION, "cap reached: the interview moves on"


@pytest.mark.asyncio
async def test_a_live_decider_cannot_keep_the_candidate_in_the_warm_up_past_the_cap(session_state_factory, monkeypatch) -> None:
    """LIVE: the model chose continue_warm_up twelve times; past the cap the
    action is not in the menu, so its choice is rejected and the rule moves on."""
    from src.modules.interviews.core.conversation.turn_context import Decision

    async def insists(ctx, *, allowed_actions, **_kw):
        assert "continue_warm_up" not in allowed_actions
        return Decision("continue_warm_up", {"follow_up_text": "Bạn có thể nói thêm về lý do bạn quan tâm vị trí không?",
                                             "missing_aspect": "lý do"}, reason="still missing", source="agent")

    monkeypatch.setattr(interview_agent, "decide_by_model", insists)
    llm = CountingLLM(evaluation={"intent": "VALID_ANSWER", "sufficiency_status": "INSUFFICIENT",
                                  "missing_aspect": "lý do", "acknowledgement": "Cảm ơn bạn.", "score": 4.0})
    output = await interview_agent.run_turn(
        InterviewCoreEngine(llm_client=llm),
        CandidateTurnInput(session_id="sess-warm", turn_index=0, text_content="Dạ em là sinh viên năm cuối."),
        session_state_factory(stage=InterviewStage.WARM_UP, clarify_used=2, elapsed_ratio=0.3),
        mode=interview_agent.MODE_LIVE,
    )
    assert output.action != TurnAction.CLARIFY
    assert output.metadata["decision"]["executed"] == "rule"
    assert output.metadata["decision"]["transition"]["validation"] == "rejected:outside_menu"
