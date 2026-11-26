"""The interview flows like a conversation (session 0e3c14c6, 2026-11-26).

The third question of that voice interview never ended: a follow-up about
"mã lỗi 404 khi tải tăng mạnh" the candidate could not place, rephrased twice,
then repeated word for word after the candidate asked "em có thể chuyển câu hỏi
khác không ạ?". Every follow-up read like a new question, the questions came
with no link to the previous answer, and the report failed because the grader
called every readable answer UNINTELLIGIBLE.
"""

from __future__ import annotations

import pytest

from src.modules.interviews.agents.evaluator.evaluation_types import TurnEvaluationInput
from src.modules.interviews.agents.evaluator.grading import InterviewEvaluationEngine, readable_transcript
from src.modules.interviews.agents.interviewer.engine import InterviewCoreEngine
from src.modules.interviews.agents.interviewer.menu import build_menu
from src.modules.interviews.agents.interviewer.prompts.decide import decide_prompt
from src.modules.interviews.agents.interviewer.prompts.probe import probe_prompt
from src.modules.interviews.agents.interviewer.thinker import decide_by_model
from src.modules.interviews.core.contracts.interview import CandidateTurnInput, InterviewStage, TurnAction
from src.modules.interviews.core.conversation.turn_context import Decision, build_turn_context, deepening_allowed
from src.modules.interviews.core.guardrails.output import announces_process, has_lead_in
from tests.modules.interviews.agent.conftest import FakeToolCaller, tool_call

SKIP = (
    "Dạ ạ, dạ em em chưa rõ cái câu hỏi này nên là em có thể xin phép là em không trả lời phần này ạ. "
    "Dạ em có thể chuyển câu hỏi khác không ạ?"
)
WARM_UP_ANSWER = (
    "Dạ hiện tại thì em đang là sinh viên của ngành kỹ thuật phần mềm tại trường đại học, hiện tại thì em có "
    "định hướng là phát triển trong lĩnh vực AI và backend developer, đặc biệt là xây dựng các hệ thống có tích "
    "hợp trí tuệ nhân tạo, em quan tâm vị trí này vì muốn áp dụng kiến thức vào môi trường doanh nghiệp."
)
MENU = ["clarify_question", "acknowledge_and_probe", "move_to_next_question"]
PACING = {"pace": "ahead", "questions_remaining_by_stage": {"BEHAVIORAL": 1}}


# ------------------------------------------------------------- the stuck question

@pytest.mark.asyncio
async def test_a_skip_request_moves_on_before_any_model_in_live_mode(session_state_factory) -> None:
    engine = InterviewCoreEngine()
    state = session_state_factory(stage=InterviewStage.DEEP_DIVE, elapsed_ratio=0.44)
    ctx = build_turn_context(CandidateTurnInput(session_id="s", turn_index=2, text_content=SKIP), state)
    early = await engine._guards_in(ctx)
    assert early is not None and early.action == "move_to_next_question"
    assert early.args["reason"] == "candidate_skip"


def test_clarifying_one_question_is_bounded(ctx_factory) -> None:
    assert "clarify_question" in build_menu(ctx_factory(clarify_used=1)).allowed
    capped = build_menu(ctx_factory(clarify_used=2))
    assert "clarify_question" not in capped.allowed and "move_to_next_question" in capped.allowed
    assert capped.reasons["clarify_question"] == "clarified 2 times"


def test_the_prompt_moves_on_when_the_candidate_cannot_answer() -> None:
    assert "xin đổi câu: chọn `move_to_next_question`" in decide_prompt(True)
    assert "asks to move on: choose `move_to_next_question`" in decide_prompt(False)


# ------------------------------------------------------------- follow-ups

def test_deepen_is_the_challenge_tiers_first_technical_follow_up_only(ctx_factory) -> None:
    assert deepening_allowed(ctx_factory(tier="challenge", stage=InterviewStage.DEEP_DIVE))
    assert not deepening_allowed(ctx_factory(tier="basic", stage=InterviewStage.DEEP_DIVE))
    assert not deepening_allowed(ctx_factory(tier="challenge", stage=InterviewStage.VALIDATE))
    assert not deepening_allowed(ctx_factory(tier="challenge", stage=InterviewStage.DEEP_DIVE, probes_used=1))


@pytest.mark.asyncio
async def test_a_deepen_choice_outside_the_challenge_tier_is_asked_as_a_gap(ctx_factory) -> None:
    ctx = ctx_factory(tier="basic", stage=InterviewStage.DEEP_DIVE)
    caller = FakeToolCaller([tool_call("acknowledge_and_probe", kind="deepen", missing_aspect="ví dụ cho từng mã lỗi")])
    decision = await decide_by_model(ctx, allowed_actions=MENU, pacing=PACING, generate=caller)
    assert decision.args["kind"] == "gap" and "deepen_downgraded" in decision.path
    assert decision.args["missing_aspect"] == "ví dụ cho từng mã lỗi"


@pytest.mark.asyncio
async def test_a_deepen_probe_keeps_the_deciders_point(ctx_factory, monkeypatch) -> None:
    engine = InterviewCoreEngine()
    seen = {}

    async def fake_probe(**kwargs):
        seen.update(kwargs)
        return "Vì sao bạn chọn cursor thay cho offset ở đây?"

    monkeypatch.setattr(engine, "_generate_probe_question", fake_probe)
    ctx = ctx_factory(tier="challenge", stage=InterviewStage.DEEP_DIVE)
    await engine.act_acknowledge_and_probe(
        ctx, Decision("acknowledge_and_probe", {"kind": "deepen", "missing_aspect": "phân trang cursor"}, source="agent")
    )
    assert seen["missing_aspect"].startswith("phân trang cursor")
    assert "tải tăng mạnh" not in seen["missing_aspect"]


def test_the_probe_prompt_continues_the_answer_instead_of_opening_a_scenario() -> None:
    vi = probe_prompt(True)
    assert "nối vào một chi tiết cụ thể ứng viên vừa nói" in vi
    assert "Không dựng tình huống mới" in vi
    assert "Khi Redis lỗi" not in vi
    assert "pick up one specific thing the candidate just said" in probe_prompt(False)


# ------------------------------------------------------------- moving on

def test_process_announcements_and_lead_ins_are_recognised() -> None:
    assert announces_process("Giờ mình sẽ chuyển sang câu hỏi tiếp theo nhé.")
    assert announces_process("Mình sang phần tiếp theo nhé.")
    assert not announces_process("Repository với dependency injection là phần bạn nắm khá chắc rồi.")
    assert has_lead_in("Tiếp theo, mình muốn hỏi về dự án trong CV của bạn.\n\nBạn đã làm gì?")
    assert has_lead_in("Mình muốn hỏi về dự án. Bạn đã làm gì?")
    assert not has_lead_in("Bạn có thể giải thích lý do chọn mã lỗi 400 hay 404 không?")


@pytest.mark.asyncio
async def test_every_move_may_carry_a_bridge_that_closes_the_thread(ctx_factory) -> None:
    bridge = "Mình hiểu rồi, repository với dependency injection là cách bạn giữ code dễ bảo trì."
    ctx = ctx_factory(stage=InterviewStage.VALIDATE, current_question={
        "question_id": "q-val-1", "stage": "VALIDATE", "competency": "cv-validation", "main_prompt": "Bạn làm gì trong dự án?",
    })
    caller = FakeToolCaller([tool_call("move_to_next_question", reason="sufficient", transition_text=bridge)])
    decision = await decide_by_model(ctx, allowed_actions=MENU, pacing=PACING, generate=caller)
    assert decision.args.get("transition_text") == bridge
    assert "transition_text" in str(caller.inputs[0]["tools"])
    output = await InterviewCoreEngine().act_move_to_next_question(ctx, decision)
    assert output.action == TurnAction.NEXT_QUESTION
    head, _, question = output.message_text.partition("\n\n")
    if question:  # the fixture's next question has no lead-in of its own
        assert head == bridge and output.metadata["transition"] == bridge


@pytest.mark.asyncio
async def test_a_narrating_bridge_is_rejected_by_the_decider(ctx_factory) -> None:
    ctx = ctx_factory(stage=InterviewStage.WARM_UP, clarify_used=1, text=WARM_UP_ANSWER)
    caller = FakeToolCaller([tool_call(
        "move_to_next_question", reason="sufficient", transition_text="Giờ mình sẽ chuyển sang câu hỏi tiếp theo nhé.",
    )])
    decision = await decide_by_model(ctx, allowed_actions=["continue_warm_up", "move_to_next_question"],
                                     pacing=PACING, generate=caller)
    assert "transition_text" not in decision.args and "transition_text_rejected" in decision.path


@pytest.mark.parametrize(
    ("spoken", "kept"),
    [
        ("Tiếp theo, mình muốn hỏi về dự án trong CV của bạn.\n\nBạn đã làm gì trong dự án đó?", False),
        ("Bạn đã làm gì trong dự án đó?", True),
    ],
)
@pytest.mark.asyncio
async def test_a_bridge_is_dropped_when_the_question_has_its_own_lead_in(ctx_factory, spoken: str, kept: bool) -> None:
    from types import SimpleNamespace

    bridge = "Mình hiểu rồi, AI và backend là hướng bạn đang đi."
    ctx = ctx_factory(stage=InterviewStage.WARM_UP, text=WARM_UP_ANSWER)
    ctx.next_stage = InterviewStage.VALIDATE
    ctx.next_question = SimpleNamespace(
        spoken_prompt=spoken, main_prompt=spoken, competency="cv-validation", question_id="q-val-1",
    )
    output = await InterviewCoreEngine().act_move_to_next_question(ctx, Decision(
        "move_to_next_question", {"reason": "sufficient", "transition_text": bridge}, source="agent",
    ))
    assert (output.metadata["transition"] == bridge) is kept
    assert output.message_text == (f"{bridge}\n\n{spoken}" if kept else spoken)


# ------------------------------------------------------------- the report

def test_a_readable_transcript_is_recognised() -> None:
    assert readable_transcript(WARM_UP_ANSWER)
    assert not readable_transcript("ừ Ừm. dạ thì ờ")


@pytest.mark.asyncio
async def test_the_grader_cannot_call_a_readable_answer_unintelligible_on_first_look(monkeypatch) -> None:
    grader = InterviewEvaluationEngine()
    asked: list[str] = []

    async def ask(instructions, user_content):
        asked.append(instructions)
        if len(asked) == 1:
            return {"answer_status": "UNINTELLIGIBLE", "score": 0.0, "feedback": "Câu trả lời không rõ ràng."}
        return {"answer_status": "ANSWERED", "score": 6.5, "feedback": "Giới thiệu được ngành học và định hướng."}

    monkeypatch.setattr(grader, "_ask_grader", ask)
    turn = TurnEvaluationInput(
        turn_id="t0", turn_index=0, question_prompt="Bạn giới thiệu ngắn về bản thân?", candidate_answer=WARM_UP_ANSWER,
        competency="Giới thiệu & Khởi động", stage="WARM_UP", source_modality="VOICE",
    )
    result = await grader.evaluate_turn(turn)
    assert len(asked) == 2 and "Không dùng UNINTELLIGIBLE" in asked[1]
    assert result.outcome == "ANSWERED" and result.score == 6.5


@pytest.mark.asyncio
async def test_noise_can_still_be_unintelligible(monkeypatch) -> None:
    grader = InterviewEvaluationEngine()
    calls = 0

    async def ask(instructions, user_content):
        nonlocal calls
        calls += 1
        return {"answer_status": "UNINTELLIGIBLE", "score": 0.0, "feedback": "x"}

    monkeypatch.setattr(grader, "_ask_grader", ask)
    turn = TurnEvaluationInput(
        turn_id="t1", turn_index=2, question_prompt="Q?", candidate_answer="ừ ừm dạ ờ in in in", competency="REST API",
        stage="DEEP_DIVE", source_modality="VOICE",
    )
    result = await grader.evaluate_turn(turn)
    assert calls == 1 and result.outcome == "NOT_GRADABLE"
