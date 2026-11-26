"""T1.1.1–T1.1.10 — build_menu filters the closed action set by the state of the turn."""

from __future__ import annotations

import itertools

import pytest

from src.modules.interviews.agents.interviewer.menu import build_menu
from src.modules.interviews.core.contracts.interview import InterviewStage
from src.modules.interviews.core.conversation.turn_context import (
    ACTIONS,
)


def test_t1_1_1_deep_dive_with_budget_offers_probe_next_and_conversation(ctx_factory) -> None:
    menu = build_menu(ctx_factory(stage=InterviewStage.DEEP_DIVE, probes_used=0, elapsed_ratio=0.5))
    for action in ("acknowledge_and_probe", "move_to_next_question", "clarify_question",
                   "answer_candidate_question", "repeat_question", "redirect_off_topic", "confirm_abort"):
        assert action in menu.allowed, action
    assert "continue_warm_up" not in menu.allowed
    assert "close_interview" not in menu.allowed and menu.reasons["close_interview"] == "questions remain"


def test_probe_remains_available_after_multiple_followups(ctx_factory) -> None:
    menu = build_menu(ctx_factory(probes_used=8))
    assert "acknowledge_and_probe" in menu.allowed


def test_t1_1_3_behind_schedule_leaves_only_moving_on(ctx_factory) -> None:
    menu = build_menu(ctx_factory(stage=InterviewStage.VALIDATE, elapsed_ratio=0.9,
                                  current_question={"question_id": "q-val-2", "stage": "VALIDATE",
                                                    "main_prompt": "?", "competency": "cv"}))
    assert "acknowledge_and_probe" not in menu.allowed
    assert "behind_schedule" in menu.reasons["acknowledge_and_probe"]
    assert "move_to_next_question" in menu.allowed


def test_t1_1_4_warm_up_offers_continuation_never_probe(ctx_factory) -> None:
    menu = build_menu(ctx_factory(stage=InterviewStage.WARM_UP, asked=("q-warm",), elapsed_ratio=0.05))
    assert "continue_warm_up" in menu.allowed
    assert "acknowledge_and_probe" not in menu.allowed
    assert "clarify_question" not in menu.allowed


def test_warm_up_continuation_is_bounded_by_the_blueprint_cap(ctx_factory) -> None:
    """Replaced on 2026-11-26: this used to assert continuation after eight
    follow-ups. Session e52c0757 showed where that leads (twelve repeats of
    one question); the cap is the blueprint's warm_up.max_follow_ups."""
    menu = build_menu(ctx_factory(stage=InterviewStage.WARM_UP, clarify_used=8, elapsed_ratio=0.05))
    assert "continue_warm_up" not in menu.allowed
    assert "move_to_next_question" in menu.allowed


def test_clarification_remains_available_after_multiple_requests(ctx_factory) -> None:
    assert "clarify_question" in build_menu(ctx_factory(clarify_used=0)).allowed
    menu = build_menu(ctx_factory(clarify_used=1))
    assert "clarify_question" in menu.allowed


def test_t1_1_6_blueprint_can_turn_probes_off(ctx_factory) -> None:
    menu = build_menu(ctx_factory(blueprint={"technical": {"probeEnabled": False}}))
    assert "acknowledge_and_probe" not in menu.allowed
    assert "blueprint_probes_off" in menu.reasons["acknowledge_and_probe"]


def test_t1_1_7_closing_has_its_own_menu(ctx_factory) -> None:
    menu = build_menu(ctx_factory(stage=InterviewStage.CLOSING, elapsed_ratio=0.96))
    assert "closing_qna" in menu.allowed
    for action in ("acknowledge_and_probe", "move_to_next_question", "clarify_question", "continue_warm_up"):
        assert action not in menu.allowed


def test_t1_1_8_memory_search_follows_its_flag(ctx_factory) -> None:
    assert "search_candidate_memory" not in build_menu(ctx_factory()).allowed
    assert "search_candidate_memory" in build_menu(ctx_factory(), memory_enabled=True).allowed


def test_t1_1_9_close_is_offered_only_when_the_queue_is_empty(ctx_factory) -> None:
    every = tuple(q["question_id"] for stage in ("WARM_UP", "VALIDATE", "DEEP_DIVE", "CHALLENGE", "BEHAVIORAL")
                  for q in __import__("tests.modules.interviews.agent.conftest", fromlist=["POOL"]).POOL[stage])
    assert "close_interview" in build_menu(ctx_factory(asked=every)).allowed


@pytest.mark.parametrize(
    ("stage", "elapsed_ratio", "probes_used", "clarify_used", "is_demo"),
    list(itertools.product(
        [InterviewStage.WARM_UP, InterviewStage.VALIDATE, InterviewStage.DEEP_DIVE, InterviewStage.CHALLENGE,
         InterviewStage.BEHAVIORAL, InterviewStage.CLOSING],
        [0.05, 0.5, 0.85, 0.99],
        [0, 1, 2],
        [0, 1],
        [False, True],
    )),
)
def test_t1_1_10_the_menu_is_never_empty_and_accounts_for_every_action(
    ctx_factory, stage, elapsed_ratio, probes_used, clarify_used, is_demo
) -> None:
    menu = build_menu(ctx_factory(stage=stage, elapsed_ratio=elapsed_ratio, probes_used=probes_used,
                                  clarify_used=clarify_used, is_demo=is_demo))
    assert menu.allowed
    assert "confirm_abort" in menu.allowed
    for action in ACTIONS:
        assert (action in menu.allowed) != (action in menu.reasons), action
