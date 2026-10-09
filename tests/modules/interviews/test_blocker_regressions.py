import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

import src.modules.interviews.api.router  # noqa: F401  (ensures the submodule is imported)
from src.modules.interviews.adapters.voice.lab import _load_voice_session
from src.modules.interviews.evaluation.evaluation_service import (
    EvaluationServiceError,
    evaluate_closed_session,
)
from src.modules.interviews.evaluation.evaluation_types import (
    DecisionRecommendation,
    SessionEvaluationResult,
)
from src.modules.interviews.planning import planner
from src.modules.interviews.planning.planner import PlannerPolicyConfigurationError

# `api/__init__.py` re-exports the APIRouter as `router`, which shadows the
# submodule attribute, so reach the module through sys.modules instead.
router_module = sys.modules["src.modules.interviews.api.router"]


def _query_result(row: dict) -> MagicMock:
    result = MagicMock()
    result.mappings.return_value.one_or_none.return_value = row
    return result


@pytest.mark.asyncio
async def test_livekit_media_loader_accepts_video_sessions() -> None:
    db = AsyncMock()
    db.execute.return_value = _query_result(
        {
            "id": "session-1",
            "user_id": "user-1",
            "mode": "video",
            "status": "OPEN",
            "plan_status": "LOCKED",
        }
    )

    session = await _load_voice_session(db, "session-1", "user-1")

    assert session["mode"] == "video"


@pytest.mark.asyncio
async def test_evaluation_rejects_an_open_session() -> None:
    db = AsyncMock()
    db.execute.return_value = _query_result(
        {
            "id": "session-1",
            "user_id": "user-1",
            "status": "OPEN",
            "locale": "vi-VN",
        }
    )

    with pytest.raises(EvaluationServiceError, match="phải kết thúc"):
        await evaluate_closed_session(db, "session-1")


def test_dynamic_planner_requires_explicit_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        planner,
        "get_settings",
        lambda: SimpleNamespace(
            interview_dynamic_planner_enabled=True,
            interview_planner_policy_json=None,
        ),
    )

    with pytest.raises(RuntimeError, match="INTERVIEW_PLANNER_POLICY_JSON is required"):
        planner.configured_planner_policy()


def test_dynamic_planner_loads_valid_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    raw_policy = """{
      "t_onboarding_base": 120,
      "n_onboarding": 1,
      "t_cv_addon_inclusive": 0,
      "t_cv_standalone_reserve": 60,
      "t_behavioral": 180,
      "probe_pool_ratio": 0.15,
      "t_closing_reserve": 60,
      "t_arch_text_min": 180,
      "t_arch_text_max": 300,
      "t_arch_code_min": 360,
      "t_arch_code_max": 600,
      "coding_fallback_policy": "downgrade_to_text",
      "strict_priority_stop": true,
      "strict_hands_on_required": null
    }"""
    monkeypatch.setattr(
        planner,
        "get_settings",
        lambda: SimpleNamespace(
            interview_dynamic_planner_enabled=True,
            interview_planner_policy_json=raw_policy,
        ),
    )

    policy = planner.configured_planner_policy()

    assert policy is not None
    assert policy.coding_fallback_policy == "downgrade_to_text"


@pytest.mark.asyncio
async def test_unasked_turns_are_not_scored_as_missing_answers(monkeypatch) -> None:
    """A PLANNED turn was never put to the candidate, so it must not be scored.

    The pacing cutoff now closes the session instead of dead-ending it, which
    leaves PLANNED turns behind. Feeding those to the grader would hand the
    candidate a "did not answer" for a question the system chose not to ask.
    """
    captured: dict[str, object] = {}

    class _Engine:
        async def evaluate_session(self, *, session_id, turns_input, is_vi):
            captured["turn_ids"] = [t.turn_id for t in turns_input]
            return SimpleNamespace(
                overall_score=7.0,
                decision_recommendation=DecisionRecommendation.PASS,
                competency_scores=[],
                turn_evaluations=[],
                recruiter_summary="s",
                candidate_feedback="f",
                next_round_topics=[],
                red_flags=[],
            )

    rows = {
        "session": {
            "id": "sess-eval",
            "status": "CLOSED",
            "locale": "vi-VN",
            "duration_minutes": 25,
        },
        "turns": [
            {
                "id": "t-answered",
                "turn_index": 0,
                "status": "ANSWERED",
                "question_snapshot": {"stage": "DEEP_DIVE", "questionText": "Q1"},
                "answer_text": "Câu trả lời thật.",
            },
            {
                "id": "t-asked-no-answer",
                "turn_index": 1,
                "status": "ASKED",
                "question_snapshot": {"stage": "DEEP_DIVE", "questionText": "Q2"},
                "answer_text": None,
            },
            {
                "id": "t-never-asked",
                "turn_index": 2,
                "status": "PLANNED",
                "question_snapshot": {
                    "stage": "BEHAVIORAL",
                    "questionText": "Q3",
                    "taxonomyTarget": {"label": "Kỹ năng mềm"},
                },
                "answer_text": None,
            },
        ],
    }

    db = AsyncMock()

    async def _execute(query, params=None):
        sql = str(query)
        result = MagicMock()
        if "FROM interview_sessions" in sql:
            result.mappings.return_value.one_or_none.return_value = rows["session"]
        elif "FROM interview_turns" in sql:
            result.mappings.return_value.all.return_value = rows["turns"]
        else:
            result.mappings.return_value.all.return_value = []
            result.mappings.return_value.one_or_none.return_value = None
        return result

    db.execute.side_effect = _execute
    db.scalar.return_value = None

    result = await evaluate_closed_session(db, "sess-eval", _Engine())

    # Neither the unasked turn nor the one cut off unanswered is graded...
    assert captured["turn_ids"] == ["t-answered"]
    # ...and is reported as uncovered rather than silently dropped.
    assert any("Kỹ năng mềm" in topic for topic in result.next_round_topics)


@pytest.mark.asyncio
async def test_planner_misconfiguration_is_a_server_error_not_a_conflict() -> None:
    """A bad deployment setting must not read as a conflict on the candidate's session.

    It also must not leak the setting name to the client.
    """
    session = {"id": "sess-cfg", "plan_id": "plan-cfg", "resume_id": "cv", "job_id": "job"}

    async def _owned(*_args, **_kwargs):
        return session

    async def _boom(**_kwargs):
        raise PlannerPolicyConfigurationError(
            "INTERVIEW_PLANNER_POLICY_JSON is required when dynamic interview planning is enabled"
        )

    with (
        patch.object(router_module, "_owned_session", _owned),
        patch.object(router_module, "run_interview_command", _boom),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await router_module.build_interview_plan(
                session_id="sess-cfg", user={"sub": "u1"}, db=AsyncMock()
            )

    assert exc_info.value.status_code == 500
    assert "INTERVIEW_PLANNER_POLICY_JSON" not in str(exc_info.value.detail)


def _eval_db(session_row, turns):
    db = AsyncMock()

    async def _execute(query, params=None):
        sql = str(query)
        result = MagicMock()
        if "FROM interview_sessions" in sql:
            result.mappings.return_value.one_or_none.return_value = session_row
        elif "FROM interview_turns" in sql:
            result.mappings.return_value.all.return_value = turns
        else:
            result.mappings.return_value.all.return_value = []
            result.mappings.return_value.one_or_none.return_value = None
        return result

    db.execute.side_effect = _execute
    db.scalar.return_value = None
    return db


class _RecordingEngine:
    """Grader stub that reports whatever decision the test asks for."""

    def __init__(self, decision="PASS"):
        self._decision = decision
        self.turn_ids: list[str] = []

    async def evaluate_session(self, *, session_id, turns_input, is_vi):
        self.turn_ids = [t.turn_id for t in turns_input]
        return SessionEvaluationResult(
            session_id=session_id,
            overall_score=8.0,
            decision_recommendation=DecisionRecommendation(self._decision),
            competency_scores=[],
            turn_evaluations=[],
            recruiter_summary="s",
            candidate_feedback="f",
            next_round_topics=[],
            red_flags=[],
        )


@pytest.mark.asyncio
async def test_evaluation_refuses_a_session_with_nothing_scorable() -> None:
    """Opening the room and quitting leaves only the zero-weight WARM_UP turn.

    Scoring that produced a 0.0 / REJECT report for a candidate who was never
    actually assessed.
    """
    session_row = {"id": "s1", "status": "CLOSED", "locale": "vi-VN", "duration_minutes": 25}
    turns = [
        {
            "id": "t-warm",
            "turn_index": 0,
            "status": "ASKED",
            "question_snapshot": {"stage": "WARM_UP", "questionText": "Giới thiệu bản thân"},
            "answer_text": None,
        },
        {
            "id": "t-deep",
            "turn_index": 1,
            "status": "PLANNED",
            "question_snapshot": {"stage": "DEEP_DIVE", "questionText": "Q"},
            "answer_text": None,
        },
    ]

    with pytest.raises(EvaluationServiceError, match="chuyên môn"):
        await evaluate_closed_session(_eval_db(session_row, turns), "s1", _RecordingEngine())


@pytest.mark.asyncio
async def test_partial_coverage_caps_the_hiring_recommendation() -> None:
    """One good answer out of a five-turn agenda must not read as a PASS."""
    session_row = {"id": "s2", "status": "CLOSED", "locale": "vi-VN", "duration_minutes": 25}
    turns = [
        {
            "id": "t-answered",
            "turn_index": 0,
            "status": "ANSWERED",
            "question_snapshot": {"stage": "DEEP_DIVE", "questionText": "Q1"},
            "answer_text": "Một câu trả lời tốt.",
        },
    ] + [
        {
            "id": f"t-planned-{i}",
            "turn_index": i,
            "status": "PLANNED",
            "question_snapshot": {
                "stage": "DEEP_DIVE",
                "questionText": f"Q{i}",
                "taxonomyTarget": {"label": f"Concept {i}"},
            },
            "answer_text": None,
        }
        for i in range(1, 5)
    ]

    result = await evaluate_closed_session(
        _eval_db(session_row, turns), "s2", _RecordingEngine("PASS")
    )

    # 1 of 5 planned turns assessed -> below the confidence threshold.
    assert result.decision_recommendation is DecisionRecommendation.CONSIDER
    assert any("1/5" in flag for flag in result.red_flags)


@pytest.mark.asyncio
async def test_full_coverage_keeps_the_recommendation() -> None:
    """With nothing uncovered the decision must pass through untouched."""
    session_row = {"id": "s3", "status": "CLOSED", "locale": "vi-VN", "duration_minutes": 25}
    turns = [
        {
            "id": "t-answered",
            "turn_index": 0,
            "status": "ANSWERED",
            "question_snapshot": {"stage": "DEEP_DIVE", "questionText": "Q1"},
            "answer_text": "Một câu trả lời tốt.",
        }
    ]

    result = await evaluate_closed_session(
        _eval_db(session_row, turns), "s3", _RecordingEngine("PASS")
    )

    assert result.decision_recommendation is DecisionRecommendation.PASS
    assert result.red_flags == []


def test_close_endpoint_refuses_a_completed_label() -> None:
    """/close bypasses the agenda-coverage invariant, so it must not accept COMPLETED."""
    with pytest.raises(ValidationError):
        router_module.CloseInterviewSession(reason="COMPLETED")

    assert router_module.CloseInterviewSession().reason == "USER_ENDED"
    assert router_module.CloseInterviewSession(reason="TECHNICAL_FAILURE").reason == (
        "TECHNICAL_FAILURE"
    )


@pytest.mark.asyncio
async def test_closing_qna_and_unanswered_turn_are_not_graded() -> None:
    """H7: the closing Q&A is not an assessed answer.

    M4: a question still on screen when the session ended is uncovered, not a
    graded "did not answer".
    """
    session_row = {"id": "s4", "status": "CLOSED", "locale": "vi-VN", "duration_minutes": 25}
    turns = [
        {
            "id": "t-answered",
            "turn_index": 0,
            "status": "ANSWERED",
            "question_snapshot": {"stage": "DEEP_DIVE", "questionText": "Q1"},
            "answer_text": "Một câu trả lời tốt.",
        },
        {
            "id": "t-cut-off",
            "turn_index": 1,
            "status": "ASKED",
            "question_snapshot": {
                "stage": "DEEP_DIVE",
                "questionText": "Q2",
                "taxonomyTarget": {"label": "Concept 2"},
            },
            "answer_text": None,
        },
        {
            "id": "t-closing",
            "turn_index": 2,
            "status": "ANSWERED",
            "question_snapshot": {"stage": "CLOSING", "questionText": "Bạn có câu hỏi gì không?"},
            "answer_text": "Công ty dùng CI/CD thế nào ạ?",
        },
    ]
    engine = _RecordingEngine("PASS")

    result = await evaluate_closed_session(_eval_db(session_row, turns), "s4", engine)

    assert engine.turn_ids == ["t-answered"]
    assert any("Concept 2" in topic for topic in result.next_round_topics)


@pytest.mark.asyncio
async def test_grading_failure_surfaces_as_503_through_the_agent_graph() -> None:
    """H6: an ungradable session is a retryable 503, not a 500 or a fake report."""
    from src.modules.interviews.application import agent_runtime
    from src.modules.interviews.evaluation.evaluation_engine import EvaluationGradingError

    session = {"id": "sess-grade", "plan_id": "plan-grade", "status": "CLOSED"}

    async def _owned(*_args, **_kwargs):
        return session

    async def _no_report(**_kwargs):
        return None

    async def _grading_fails(**_kwargs):
        raise EvaluationGradingError("Không chấm được 2/3 lượt trả lời. Vui lòng thử lại sau.")

    with (
        patch.object(router_module, "_owned_session", _owned),
        patch.object(agent_runtime, "get_session_evaluation", _no_report),
        patch.object(agent_runtime, "evaluate_closed_session", _grading_fails),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await router_module.evaluate_session_endpoint(
                session_id="sess-grade", user={"sub": "u1"}, db=AsyncMock()
            )

    assert exc_info.value.status_code == 503
    assert "thử lại" in exc_info.value.detail


@pytest.mark.asyncio
async def test_concurrent_evaluation_reuses_the_report_created_while_waiting() -> None:
    """M6: the second /evaluate waits on the session lock, then must not regrade."""
    session_row = {"id": "s5", "status": "CLOSED", "locale": "vi-VN", "duration_minutes": 25}
    db = _eval_db(session_row, [])
    db.scalar.return_value = 1  # report committed by the first request
    engine = _RecordingEngine("PASS")

    assert await evaluate_closed_session(db, "s5", engine) is None
    assert engine.turn_ids == []
    locked = [str(call.args[0]) for call in db.execute.call_args_list if "FOR UPDATE" in str(call.args[0])]
    assert locked, "session row must be locked before checking for an existing report"


@pytest.mark.asyncio
async def test_competency_weights_follow_planner_importance() -> None:
    """M5: must-have/heavier competencies weigh more; presets weigh half."""
    session_row = {"id": "s6", "status": "CLOSED", "locale": "vi-VN", "duration_minutes": 25}

    def turn(idx, stage, concept=None):
        snap = {"stage": stage, "questionText": f"Q{idx}"}
        if concept:
            snap["taxonomyTarget"] = {"conceptId": concept, "label": concept}
        return {"id": f"t{idx}", "turn_index": idx, "status": "ANSWERED",
                "question_snapshot": snap, "answer_text": "Câu trả lời."}

    turns = [
        turn(0, "WARM_UP"), turn(1, "VALIDATE"),
        turn(2, "DEEP_DIVE", "skill-python"), turn(3, "DEEP_DIVE", "skill-sql"),
        turn(4, "BEHAVIORAL"),
    ]
    db = _eval_db(session_row, turns)
    base_execute = db.execute.side_effect

    async def _execute(query, params=None):
        if "session_competency_targets" in str(query):
            result = MagicMock()
            result.mappings.return_value.all.return_value = [
                {"concept_id": "skill-python", "importance": 0.75},
                {"concept_id": "skill-sql", "importance": 0.25},
            ]
            return result
        return await base_execute(query, params)

    db.execute.side_effect = _execute
    weights: dict[str, float] = {}

    class _Engine(_RecordingEngine):
        async def evaluate_session(self, *, session_id, turns_input, is_vi):
            weights.update({t.turn_id: t.weight for t in turns_input})
            return await super().evaluate_session(session_id=session_id, turns_input=turns_input, is_vi=is_vi)

    await evaluate_closed_session(db, "s6", _Engine())

    assert weights == {"t0": 0.0, "t1": 0.5, "t2": 1.5, "t3": 0.5, "t4": 0.5}
