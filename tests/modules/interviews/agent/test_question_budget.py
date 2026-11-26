"""One question cannot take the interview (session 2678ce7b, 2026-11-26).

The first CV question of a fifteen-minute voice interview was followed up nine
times in ten minutes: project role, AI trade-offs, load, Redis, queues, rate
limits. Three causes, each tested here:

- nothing measured a question as a whole -- ``hard_answer_timeout`` is the
  length of one answer, and 3bf4591 had removed the per-question budget;
- after the first follow-up the decider's "question" was its own follow-up and
  the "answer" the latest fragment, so it followed up on its follow-ups;
- a rejected clarification raised out of the turn and the session closed as a
  technical failure, without a report.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.modules.interviews.agents.interviewer.engine import InterviewCoreEngine
from src.modules.interviews.agents.interviewer.menu import build_menu
from src.modules.interviews.agents.interviewer.pacing import pacing_block
from src.modules.interviews.agents.interviewer.prompts.decide import decide_prompt, question_exchange_block
from src.modules.interviews.agents.interviewer.thinker import decide_by_model
from src.modules.interviews.core.contracts.interview import InterviewStage, TurnAction
from src.modules.interviews.core.conversation.turn_context import Decision, probe_window, question_time_budget
from src.modules.interviews.core.conversation.turn_rules import follow_up_repeats, warm_up_follow_up_repeats
from tests.modules.interviews.agent.conftest import POOL, FakeToolCaller, _question, tool_call

QUESTION = _question("q-val-1", "VALIDATE", "cv-validation") | {
    "main_prompt": 'Bạn có thể cho biết về vai trò cụ thể của mình trong dự án "Website đánh giá hồ sơ xin việc" không?',
}
# Everything asked but one technical and one behavioural question, as in the session.
ASKED = tuple(
    str(q["question_id"]) for stage in POOL.values() for q in stage if q["question_id"] not in {"q-dd-3", "q-bh-2"}
)
PROBE_1 = "Khi bạn tích hợp AI vào hệ thống, bạn đã phải đánh đổi điều gì trong thiết kế kiến trúc Backend?"
PROBE_2 = "Khi hệ thống gặp tải cao, bạn đã chuẩn bị giải pháp nào để xử lý tình huống đó?"
PACING = {"pace": "on_track", "questions_remaining_by_stage": {"DEEP_DIVE": 1, "BEHAVIORAL": 1}}
MENU = ["clarify_question", "acknowledge_and_probe", "move_to_next_question"]


def _validate_ctx(ctx_factory, *, on_question: int | None, elapsed_ratio: float, **extra):
    started = (
        {"question_started_at": datetime.now(timezone.utc) - timedelta(seconds=on_question)}
        if on_question is not None
        else {}
    )
    return ctx_factory(
        text="Dạ em dùng hàng đợi và Redis để giảm tải.",
        stage=InterviewStage.VALIDATE, target_minutes=15, elapsed_ratio=elapsed_ratio,
        asked=ASKED, current_question=QUESTION, extra={**started, **extra},
    )


def test_a_question_without_a_start_time_keeps_the_old_behaviour(ctx_factory) -> None:
    ctx = _validate_ctx(ctx_factory, on_question=None, elapsed_ratio=0.4)
    assert question_time_budget(ctx) is None
    assert probe_window(ctx).reasons["question_time"] is False


@pytest.mark.parametrize(
    ("on_question", "elapsed_ratio", "open_"),
    [
        (97, 0.164, True),    # 15:44:14, first follow-up
        (190, 0.27, True),    # 15:45:47
        (266, 0.353, True),   # 15:47:03, third follow-up: 266 s of a 271 s share
        (367, 0.466, False),  # 15:48:44: the share is spent, the fourth is not allowed
        (617, 0.744, False),  # 15:52:54, where the session actually was
    ],
)
def test_the_session_timeline_closes_the_probe_window_after_the_questions_share(
    ctx_factory, on_question: int, elapsed_ratio: float, open_: bool
) -> None:
    ctx = _validate_ctx(ctx_factory, on_question=on_question, elapsed_ratio=elapsed_ratio)
    used, share = question_time_budget(ctx)
    assert used == pytest.approx(on_question, abs=2)
    assert 255 <= share <= 285, share  # (used + time left before closing) / 3 questions
    window = probe_window(ctx)
    assert window.reasons["question_time"] is (not open_)
    menu = build_menu(ctx)
    assert ("acknowledge_and_probe" in menu.allowed) is open_
    assert "move_to_next_question" in menu.allowed
    if not open_:
        assert "question_time" in menu.reasons["acknowledge_and_probe"]


def test_the_decider_sees_the_questions_share_instead_of_continue_while_useful(ctx_factory) -> None:
    block = pacing_block(_validate_ctx(ctx_factory, on_question=266, elapsed_ratio=0.353))
    assert "probe_follow_ups" not in block
    assert block["question_seconds_used"] == pytest.approx(266, abs=2)
    assert 255 <= block["question_seconds_budget"] <= 285
    assert "continue while useful" not in str(block)


def test_the_exchange_block_shows_the_main_question_and_every_turn() -> None:
    block = question_exchange_block({
        "main_question": QUESTION["main_prompt"],
        "probes": 2,
        "messages": [
            {"role": "assistant", "text": QUESTION["main_prompt"]},
            {"role": "user", "text": "Em là Backend Developer, thiết kế kiến trúc và tích hợp AI."},
            {"role": "assistant", "text": PROBE_1},
            {"role": "user", "text": "Đánh đổi giữa độ chính xác, tốc độ và chi phí."},
            {"role": "assistant", "text": PROBE_2},
        ],
    })
    lines = block.split("\n")
    assert lines[0] == "Diễn biến câu hỏi này (đã hỏi đào sâu 2 lần):"
    assert lines[1] == f"Câu hỏi chính: {QUESTION['main_prompt']}"
    assert f"Bạn: {PROBE_1}" in lines and f"Bạn: {PROBE_2}" in lines
    assert block.count("<candidate_response>") == 2


@pytest.mark.asyncio
async def test_after_a_follow_up_the_decider_judges_the_main_question(ctx_factory) -> None:
    exchange = [
        {"role": "assistant", "text": QUESTION["main_prompt"]},
        {"role": "user", "text": "Em là Backend Developer, thiết kế kiến trúc và tích hợp AI."},
        {"role": "assistant", "text": PROBE_1},
    ]
    ctx = _validate_ctx(
        ctx_factory, on_question=120, elapsed_ratio=0.2,
        question_follow_ups=[PROBE_1], question_exchange=exchange,
        current_question=None,
    )
    ctx.current_question = dict(QUESTION, follow_up_prompt=PROBE_1)
    caller = FakeToolCaller([tool_call("move_to_next_question", reason="sufficient")])
    await decide_by_model(ctx, allowed_actions=MENU, pacing=PACING, generate=caller)
    sent = caller.inputs[0]["input_items"]
    assert "Diễn biến câu hỏi này (đã hỏi đào sâu 1 lần):" in sent
    assert f"Câu hỏi chính: {QUESTION['main_prompt']}" in sent
    # Before the first follow-up there is nothing to add.
    first = _validate_ctx(ctx_factory, on_question=60, elapsed_ratio=0.1)
    caller = FakeToolCaller([tool_call("move_to_next_question", reason="sufficient")])
    await decide_by_model(first, allowed_actions=MENU, pacing=PACING, generate=caller)
    assert "Diễn biến câu hỏi này" not in caller.inputs[0]["input_items"]


def test_the_prompt_anchors_follow_ups_on_the_main_question() -> None:
    vi, en = decide_prompt(True), decide_prompt(False)
    assert "CÂU HỎI CHÍNH" in vi and "không đào tiếp từ câu hỏi thêm" in vi
    assert "question_seconds_budget" in vi and "question_seconds_budget" in en
    assert "never follow up on your own previous follow-up" in en


def test_the_repeat_check_is_shared_with_the_warm_up() -> None:
    assert warm_up_follow_up_repeats is follow_up_repeats
    again = "Khi hệ thống gặp tải cao, bạn chuẩn bị giải pháp nào để xử lý tình huống đó?"
    assert follow_up_repeats(again, [PROBE_1, PROBE_2]) == "follow_up:2"
    assert follow_up_repeats(PROBE_2, [PROBE_1]) is None


@pytest.mark.asyncio
async def test_a_probe_that_asks_an_earlier_probe_again_moves_on(ctx_factory) -> None:
    ctx = _validate_ctx(ctx_factory, on_question=120, elapsed_ratio=0.2, question_follow_ups=[PROBE_1, PROBE_2])
    again = "Khi hệ thống gặp tải cao, bạn chuẩn bị giải pháp nào để xử lý tình huống đó?"
    decision = Decision("acknowledge_and_probe", {"kind": "gap", "missing_aspect": "tải", "probe_text": again}, source="agent")
    output = await InterviewCoreEngine().act_acknowledge_and_probe(ctx, decision)
    assert output.action == TurnAction.NEXT_QUESTION


@pytest.mark.asyncio
async def test_a_probe_no_guard_accepts_moves_on_instead_of_failing_the_turn(ctx_factory, monkeypatch) -> None:
    engine = InterviewCoreEngine()

    async def rejected(**_kw):
        raise ValueError("AI probe rejected by content guard")

    monkeypatch.setattr(engine, "_generate_probe_question", rejected)
    ctx = _validate_ctx(ctx_factory, on_question=60, elapsed_ratio=0.1)
    output = await engine.act_acknowledge_and_probe(
        ctx, Decision("acknowledge_and_probe", {"kind": "gap", "missing_aspect": "x"}, source="agent")
    )
    assert output.action == TurnAction.NEXT_QUESTION


@pytest.mark.asyncio
async def test_a_rejected_clarification_repeats_the_question_instead_of_ending_the_session(ctx_factory, monkeypatch) -> None:
    """15:53:50: "clarification response rejected: evaluative" -> TECHNICAL_FAILURE."""
    engine = InterviewCoreEngine()

    async def rejected(**_kw):
        raise ValueError("clarification response rejected: evaluative")

    monkeypatch.setattr(engine, "_generate_clarify_text", rejected)
    ctx = _validate_ctx(ctx_factory, on_question=60, elapsed_ratio=0.1)
    output = await engine.act_clarify_question(ctx, Decision("clarify_question", {}, source="agent"))
    assert output.action == TurnAction.CLARIFY
    assert output.message_text == ctx.heard_prompt and output.message_text
