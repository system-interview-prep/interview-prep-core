"""Interview blueprints: the default reproduces the old constants; experts' changes take effect."""

import pytest
from pydantic import ValidationError

from src.modules.interviews.application.chat_runtime import session_blueprint
from src.modules.interviews.application.interview_blueprints import _view
from src.modules.interviews.core.interview_blueprint import (
    InterviewBlueprint,
    blueprint_from_payload,
    default_blueprint,
)
from src.modules.interviews.core.interview_engine import InterviewCoreEngine
from src.modules.interviews.core.interview_rules import MAX_QNA_QUESTIONS, QNA_BUDGET_SECONDS, TIME_THRESHOLDS
from src.modules.interviews.core.interview_types import InterviewStage
from src.modules.interviews.planning.planner import _question_budget
from src.modules.interviews.planning.question_selector import _behavioral_snapshot, _technical_stage


def test_default_blueprint_keeps_every_previous_constant() -> None:
    blueprint = default_blueprint()
    assert blueprint.preset_expected_seconds() == 120 + 180 + 180 + 60
    assert blueprint.technical.expected_seconds_per_question == 30 + 150 + 60
    assert {d: _question_budget(d) for d in (3, 15, 25, 45)} == {3: 2, 15: 1, 25: 4, 45: 8}
    assert blueprint.validate_cv.until_ratio == TIME_THRESHOLDS[InterviewStage.VALIDATE]
    assert blueprint.technical.deep_dive_until_ratio == TIME_THRESHOLDS[InterviewStage.DEEP_DIVE]
    assert blueprint.technical.challenge_until_ratio == TIME_THRESHOLDS[InterviewStage.CHALLENGE]
    assert blueprint.closing.max_questions == MAX_QNA_QUESTIONS
    assert blueprint.closing.budget_seconds == QNA_BUDGET_SECONDS
    assert (blueprint.warm_up.soft_answer_seconds, blueprint.warm_up.hard_answer_seconds) == (120, 180)
    assert (blueprint.behavioral.soft_answer_seconds, blueprint.behavioral.hard_answer_seconds) == (180, 240)
    assert all(row["fits"] for row in blueprint.preview())


def test_payload_round_trip_and_fallback_to_default() -> None:
    payload = default_blueprint().to_payload()
    assert payload["validate"]["untilRatio"] == 0.30
    assert blueprint_from_payload(payload) == default_blueprint()
    assert blueprint_from_payload(None) == default_blueprint()
    assert (
        blueprint_from_payload({"warmUp": {"softAnswerSeconds": 999, "hardAnswerSeconds": 1}})
        == default_blueprint()
    )
    assert session_blueprint([{"question_snapshot": {"stage": "WARM_UP"}}]) == default_blueprint()
    frozen = {"blueprint": {"policy": {"closing": {"qnaEnabled": False}}}}
    assert session_blueprint([{"question_snapshot": frozen}]).closing.qna_enabled is False


@pytest.mark.parametrize(
    "definition",
    [
        {"warmUp": {"softAnswerSeconds": 200, "hardAnswerSeconds": 150}},
        {"validate": {"untilRatio": 0.8}},
        {"technical": {"maxQuestions": 40}},
        {"unknownStage": {}},
    ],
)
def test_invalid_definitions_are_rejected(definition: dict) -> None:
    with pytest.raises(ValidationError):
        InterviewBlueprint.model_validate(definition)


def test_turning_stages_off_frees_time_for_technical_questions() -> None:
    lean = InterviewBlueprint.model_validate(
        {"validate": {"enabled": False}, "behavioral": {"enabled": False}}
    )
    assert lean.preset_expected_seconds() == 120 + 60
    assert lean.technical_question_budget(25) == 5
    assert lean.technical_question_budget(25) > default_blueprint().technical_question_budget(25)


def test_a_blueprint_that_overflows_a_package_cannot_be_activated() -> None:
    slow = InterviewBlueprint.model_validate({"technical": {"expectedSecondsPerQuestion": 900}})
    assert [row["durationMinutes"] for row in slow.preview() if not row["fits"]] == [15]
    assert slow.activation_errors() and "15 phút" in slow.activation_errors()[0]


def test_selector_follows_the_blueprint_stage_rules() -> None:
    no_gap_challenge = InterviewBlueprint.model_validate({"technical": {"challengeForCvGaps": False}})
    assert _technical_stage(1, total=3, is_coding=False, is_gap=True, is_demo=False) == "CHALLENGE"
    assert (
        _technical_stage(1, total=3, is_coding=False, is_gap=True, is_demo=False, blueprint=no_gap_challenge)
        == "DEEP_DIVE"
    )
    longer = InterviewBlueprint.model_validate(
        {"behavioral": {"softAnswerSeconds": 240, "hardAnswerSeconds": 300}}
    )
    snapshot = _behavioral_snapshot("vi-VN", longer)
    assert (snapshot["softAnswerSeconds"], snapshot["hardAnswerSeconds"]) == (240, 300)


def _question(qid: str, stage: InterviewStage) -> dict:
    return {"question_id": qid, "stage": stage.value, "competency": "x", "main_prompt": f"Câu {qid}?"}


def _state(blueprint: dict | None, *, pool: dict, current: InterviewStage, asked: list[str]) -> dict:
    state = {
        "current_stage": current.value,
        "target_duration_minutes": 25,
        "elapsed_time": 600,
        "remaining_time": 900,
        "questions_pool": pool,
        "asked_question_ids": asked,
        "answered_question_ids": asked,
    }
    if blueprint is not None:
        state["blueprint"] = blueprint
    return state


def test_without_behavioral_the_last_technical_answer_opens_the_qna() -> None:
    engine = InterviewCoreEngine()
    pool = {InterviewStage.DEEP_DIVE.value: [_question("t1", InterviewStage.DEEP_DIVE)]}
    lean = InterviewBlueprint.model_validate({"behavioral": {"enabled": False}}).to_payload()

    stage, question = engine._get_next_stage_and_question(
        _state(lean, pool=pool, current=InterviewStage.DEEP_DIVE, asked=["t1"])
    )
    assert stage == InterviewStage.CLOSING and question is not None

    # The default still requires the Behavioral answer before the Q&A.
    stage, _ = engine._get_next_stage_and_question(
        _state(None, pool=pool, current=InterviewStage.DEEP_DIVE, asked=["t1"])
    )
    assert stage == InterviewStage.CLOSED


def test_qna_can_be_turned_off() -> None:
    engine = InterviewCoreEngine()
    pool = {InterviewStage.BEHAVIORAL.value: [_question("b1", InterviewStage.BEHAVIORAL)]}
    no_qna = InterviewBlueprint.model_validate({"closing": {"qnaEnabled": False}}).to_payload()

    stage, _ = engine._get_next_stage_and_question(
        _state(no_qna, pool=pool, current=InterviewStage.BEHAVIORAL, asked=["b1"])
    )
    assert stage == InterviewStage.CLOSED
    stage, _ = engine._get_next_stage_and_question(
        _state(None, pool=pool, current=InterviewStage.BEHAVIORAL, asked=["b1"])
    )
    assert stage == InterviewStage.CLOSING


def test_stage_ratios_come_from_the_blueprint() -> None:
    engine = InterviewCoreEngine()
    pool = {
        InterviewStage.VALIDATE.value: [_question("v2", InterviewStage.VALIDATE)],
        InterviewStage.DEEP_DIVE.value: [_question("t1", InterviewStage.DEEP_DIVE)],
    }
    # 600 / 1500 = 40% elapsed: past the default 30% VALIDATE window, inside a 50% one.
    stage, _ = engine._get_next_stage_and_question(
        _state(None, pool=pool, current=InterviewStage.VALIDATE, asked=[])
    )
    assert stage == InterviewStage.DEEP_DIVE
    wide = InterviewBlueprint.model_validate({"validate": {"untilRatio": 0.5}}).to_payload()
    stage, _ = engine._get_next_stage_and_question(
        _state(wide, pool=pool, current=InterviewStage.VALIDATE, asked=[])
    )
    assert stage == InterviewStage.VALIDATE


def _row(status: str, created_by: str) -> dict:
    return {
        "id": "v1",
        "version": 1,
        "name": "x",
        "status": status,
        "definition": {},
        "change_summary": "",
        "based_on_id": None,
        "created_by": created_by,
        "submitted_at": None,
        "reviewed_by": None,
        "reviewed_at": None,
        "review_note": None,
        "activated_at": None,
        "created_at": None,
        "updated_at": None,
    }


def test_four_eyes_rule_on_actions() -> None:
    expert, admin = "expert-1", "admin-1"
    draft = _view(_row("DRAFT", expert), actor_id=expert, roles=["QUESTION_AUTHOR"])["actions"]
    assert draft["edit"] and draft["submit"] and not draft["activate"]
    review_by_admin = _view(_row("IN_REVIEW", expert), actor_id=admin, roles=["ADMIN"])["actions"]
    assert review_by_admin["activate"] and review_by_admin["reject"]
    own_by_admin = _view(_row("IN_REVIEW", admin), actor_id=admin, roles=["ADMIN"])["actions"]
    assert not own_by_admin["activate"]
    other_expert = _view(_row("DRAFT", expert), actor_id="expert-2", roles=["QUESTION_AUTHOR"])["actions"]
    assert not other_expert["edit"]


def test_blueprint_api_requires_an_internal_role(client) -> None:
    assert client.get("/admin/interview-blueprints").status_code == 401
    assert client.post("/admin/interview-blueprints/preview", json={}).status_code == 401
    from src.modules.interviews.api.blueprint_router import router

    guards = {
        route.path: [dep.call.__qualname__ for dep in route.dependant.dependencies]
        for route in router.routes
        if route.path.endswith(("/activate", "/reject"))
    }
    assert guards and all(any("require_admin" in name for name in names) for names in guards.values())


def test_preview_endpoint_logic_reports_overflow() -> None:
    from src.modules.interviews.application.interview_blueprints import BlueprintError, _parse

    with pytest.raises(BlueprintError) as excinfo:
        _parse({"warmUp": {"softAnswerSeconds": 300, "hardAnswerSeconds": 100}})
    assert excinfo.value.code == "invalid_blueprint" and excinfo.value.detail
