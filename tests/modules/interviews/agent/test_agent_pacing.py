"""T1.1.21–T1.1.25 — the pacing block is computed by code, never guessed."""

from __future__ import annotations

from src.modules.interviews.agent.interviewer.pacing import DEFAULT_SECONDS_PER_QUESTION, pacing_block
from src.modules.interviews.core.contracts.interview import InterviewStage


def test_t1_1_21_spare_time_is_ahead(ctx_factory) -> None:
    # 8 assessed questions planned (warm-up is not one); 6 asked after 12.5 min → 125 s each;
    # the 2 left need 250 s of the 750 s remaining → ahead.
    ctx = ctx_factory(elapsed_ratio=0.5, asked=("q-warm", "q-val-1", "q-val-2", "q-dd-1", "q-dd-2", "q-dd-3", "q-ch-1"))
    block = pacing_block(ctx)
    assert block["questions_planned"] == 8 and block["questions_asked"] == 6
    assert block["questions_remaining_by_stage"] == {"BEHAVIORAL": 2}
    assert block["seconds_needed_for_remaining_plan"] == 2 * block["seconds_per_question_so_far"]
    assert block["pace"] == "ahead"


def test_t1_1_22_too_much_plan_for_the_time_left_is_behind(ctx_factory) -> None:
    ctx = ctx_factory(elapsed_ratio=0.8, asked=("q-warm", "q-val-1", "q-val-2", "q-dd-1"))
    block = pacing_block(ctx)
    assert block["questions_remaining_by_stage"] == {"DEEP_DIVE": 2, "CHALLENGE": 1, "BEHAVIORAL": 2}
    assert block["seconds_needed_for_remaining_plan"] > block["time_remaining_seconds"]
    assert block["pace"] == "behind"


def test_t1_1_23_before_any_answer_the_default_rate_is_used(ctx_factory) -> None:
    ctx = ctx_factory(stage=InterviewStage.WARM_UP, elapsed_ratio=0.0, asked=("q-warm",))
    block = pacing_block(ctx)
    assert block["questions_asked"] == 0
    assert block["seconds_per_question_so_far"] == DEFAULT_SECONDS_PER_QUESTION
    assert block["seconds_needed_for_remaining_plan"] == 8 * DEFAULT_SECONDS_PER_QUESTION


def test_t1_1_24_a_demo_is_always_on_track(ctx_factory) -> None:
    assert pacing_block(ctx_factory(elapsed_ratio=0.95, is_demo=True))["pace"] == "on_track"


def test_t1_1_25_remaining_counts_only_frozen_unasked_questions_per_stage(ctx_factory) -> None:
    block = pacing_block(ctx_factory(asked=("q-warm", "q-val-1", "q-dd-1", "q-dd-2", "q-bh-1")))
    assert block["questions_remaining_by_stage"] == {"VALIDATE": 1, "DEEP_DIVE": 1, "CHALLENGE": 1, "BEHAVIORAL": 1}
    assert block["probes_used_in_question"] == 0
    assert block["probe_follow_ups"] == "continue while useful and within interview time"
    assert block["stage"] == "DEEP_DIVE" and block["stage_until_ratio"] == 0.75
