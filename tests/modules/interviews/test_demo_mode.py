"""Demo package (3 minutes): turn-driven, two technical questions, every stage reached."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from src.modules.interviews.core.demo_mode import DEMO_OVERTIME_GRACE_SECONDS, is_demo_duration
from src.modules.interviews.core.interview_engine import InterviewCoreEngine
from src.modules.interviews.core.interview_types import (
    CandidateTurnInput,
    InterviewStage,
    SessionExitReason,
    TurnAction,
)
from src.modules.interviews.planning.planner import _CandidateTarget, _demo_targets, _question_budget
from src.modules.interviews.planning.question_selector import _technical_stage
from src.modules.user_cvs.schemas import TaxonomyRef


class _LLM:
    def __init__(self, sufficient: bool = False):
        self.sufficient = sufficient

    async def generate_json(self, system_prompt: str, user_content: str):
        return {"score": 5.0, "is_sufficient": self.sufficient, "acknowledgement": "Cảm ơn."}

    async def generate_text(self, system_prompt: str, user_content: str):
        return "Câu hỏi đào sâu."


def _q(qid: str, stage: InterviewStage) -> dict:
    return {"question_id": qid, "competency": qid, "stage": stage.value, "main_prompt": f"Câu {qid}?"}


def _target(concept_id: str, status: str) -> _CandidateTarget:
    item = _CandidateTarget(
        concept=TaxonomyRef(conceptId=concept_id, scheme="internal", taxonomyVersion="v1", label=concept_id)
    )
    item.match_statuses.append(status)
    return item


def test_only_the_demo_package_is_demo() -> None:
    assert is_demo_duration(3)
    assert not any(is_demo_duration(m) for m in (None, 0, 15, 25, 45))


def test_demo_freezes_two_technical_questions_and_other_packages_are_unchanged() -> None:
    assert {d: _question_budget(d) for d in (3, 15, 25, 45)} == {3: 2, 15: 1, 25: 4, 45: 8}


def test_demo_puts_a_cv_gap_in_the_last_slot() -> None:
    node, sql, xml = _target("node", "met"), _target("sql", "met"), _target("xml", "not_met")
    # No gap in the top two: the best-ranked gap replaces the last slot.
    assert [t.concept.concept_id for t in _demo_targets([node, sql, xml], [node, sql])] == ["node", "xml"]
    # A gap ranked first is moved last, so it becomes the CHALLENGE question.
    assert [t.concept.concept_id for t in _demo_targets([xml, node], [xml, node])] == ["node", "xml"]
    # No gap anywhere: keep the ranking.
    assert [t.concept.concept_id for t in _demo_targets([node, sql], [node, sql])] == ["node", "sql"]


def test_demo_last_technical_question_is_challenge() -> None:
    stages = [_technical_stage(i, total=2, is_coding=False, is_gap=False, is_demo=True) for i in range(2)]
    assert stages == ["DEEP_DIVE", "CHALLENGE"]
    # Regular sessions still need a gap or a coding question for CHALLENGE.
    regular = [_technical_stage(i, total=2, is_coding=False, is_gap=False, is_demo=False) for i in range(2)]
    assert regular == ["DEEP_DIVE", "DEEP_DIVE"]
    assert _technical_stage(1, total=4, is_coding=False, is_gap=True, is_demo=False) == "CHALLENGE"


def _demo_pool() -> dict:
    return {
        InterviewStage.DEEP_DIVE.value: [_q("dd", InterviewStage.DEEP_DIVE)],
        InterviewStage.CHALLENGE.value: [_q("ch", InterviewStage.CHALLENGE)],
        InterviewStage.BEHAVIORAL.value: [_q("beh", InterviewStage.BEHAVIORAL)],
    }


def test_demo_past_its_three_minutes_still_walks_challenge_behavioral_and_closing() -> None:
    engine = InterviewCoreEngine(llm_client=_LLM())
    overtime = {"target_duration_minutes": 3, "remaining_time": 0, "elapsed_time": 200, "is_demo": True}

    stage, q = engine._get_next_stage_and_question(
        {**overtime, "current_stage": "DEEP_DIVE", "questions_pool": _demo_pool(),
         "asked_question_ids": ["dd"]}
    )
    assert (stage, q.question_id) == (InterviewStage.CHALLENGE, "ch")

    stage, q = engine._get_next_stage_and_question(
        {**overtime, "current_stage": "CHALLENGE", "questions_pool": _demo_pool(),
         "asked_question_ids": ["dd", "ch"]}
    )
    assert (stage, q.question_id) == (InterviewStage.BEHAVIORAL, "beh")

    stage, q = engine._get_next_stage_and_question(
        {**overtime, "current_stage": "BEHAVIORAL", "questions_pool": _demo_pool(),
         "asked_question_ids": ["dd", "ch", "beh"], "answered_question_ids": ["dd", "ch", "beh"]}
    )
    assert stage == InterviewStage.CLOSING


def test_regular_three_minute_pacing_is_unchanged_without_the_demo_flag() -> None:
    engine = InterviewCoreEngine(llm_client=_LLM())
    stage, _ = engine._get_next_stage_and_question({
        "target_duration_minutes": 3, "remaining_time": 0, "elapsed_time": 200,
        "current_stage": "DEEP_DIVE", "questions_pool": _demo_pool(), "asked_question_ids": ["dd"],
    })
    assert stage != InterviewStage.CHALLENGE


def _turn_state(minutes_ago: float, *, is_demo: bool = True) -> dict:
    return {
        "current_stage": InterviewStage.DEEP_DIVE.value,
        "started_at": datetime.now(UTC) - timedelta(minutes=minutes_ago),
        "target_duration_minutes": 3,
        "is_demo": is_demo,
        "questions_pool": _demo_pool(),
        "asked_question_ids": ["dd"],
        "current_turn_in_question": 0,
        "current_question_context": {"question_id": "dd", "main_prompt": "Câu dd?"},
    }


def _answer() -> CandidateTurnInput:
    return CandidateTurnInput(
        session_id=uuid4(), turn_index=2, text_content="Em dùng async/await và try/catch."
    )


@pytest.mark.asyncio
async def test_demo_does_not_probe_and_does_not_time_out_inside_the_grace() -> None:
    engine = InterviewCoreEngine(llm_client=_LLM(sufficient=False))
    output = await engine.handle_turn(_answer(), _turn_state(minutes_ago=4))  # 1 minute overtime
    assert output.action == TurnAction.NEXT_QUESTION
    assert output.exit_reason != SessionExitReason.HARD_TIMEOUT
    assert output.current_stage == InterviewStage.CHALLENGE


@pytest.mark.asyncio
async def test_demo_times_out_after_the_overtime_grace() -> None:
    engine = InterviewCoreEngine(llm_client=_LLM())
    minutes = 3 + (DEMO_OVERTIME_GRACE_SECONDS + 30) / 60
    output = await engine.handle_turn(_answer(), _turn_state(minutes_ago=minutes))
    assert output.exit_reason == SessionExitReason.HARD_TIMEOUT
