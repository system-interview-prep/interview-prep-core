"""Mức luyện theo điểm matching: thử thách hỏi đào sâu cả câu trả lời đã đủ ý."""

from __future__ import annotations

import pytest

from src.modules.interviews.application.chat_runtime import build_session_state_from_db
from src.modules.interviews.core.interview_engine import DEEPEN_ASPECT_VI, InterviewCoreEngine
from src.modules.interviews.core.interview_types import CandidateTurnInput, InterviewStage, TurnAction
from tests.modules.interviews.test_interviewer_voice import LONG_ANSWER, RecordingLLM, _state


class ProbeRecordingLLM(RecordingLLM):
    def __init__(self, result):
        super().__init__(result)
        self.text_prompts: list[str] = []

    async def generate_text(self, system_prompt: str, user_content: str):
        self.text_prompts.append(user_content)
        return "Nếu lượng request tăng gấp mười thì cache này chịu thế nào?"


SUFFICIENT = {
    "intent": "ANSWER",
    "sufficiency_status": "SUFFICIENT",
    "acknowledgement": "À, vậy là cache hết hạn sau năm phút.",
    "probe_question": "",
    "score": 8,
}


@pytest.mark.asyncio
async def test_challenge_tier_deepens_a_sufficient_answer() -> None:
    llm = ProbeRecordingLLM(SUFFICIENT)
    output = await InterviewCoreEngine(llm_client=llm).handle_turn(
        CandidateTurnInput(session_id="sess-voice", turn_index=3, text_content=LONG_ANSWER),
        _state(practice_tier="challenge"),
    )
    assert output.action == TurnAction.PROBE
    assert output.metadata["probe_kind"] == "deepen"
    assert "gấp mười" in output.message_text
    assert any(DEEPEN_ASPECT_VI in prompt for prompt in llm.text_prompts)


@pytest.mark.asyncio
@pytest.mark.parametrize("tier", ["standard", "basic", None])
async def test_other_tiers_move_on_after_a_sufficient_answer(tier) -> None:
    state = _state() if tier is None else _state(practice_tier=tier)
    output = await InterviewCoreEngine(llm_client=ProbeRecordingLLM(SUFFICIENT)).handle_turn(
        CandidateTurnInput(session_id="sess-voice", turn_index=3, text_content=LONG_ANSWER), state
    )
    assert output.action == TurnAction.NEXT_QUESTION


@pytest.mark.asyncio
async def test_challenge_deepens_only_once_per_question() -> None:
    output = await InterviewCoreEngine(llm_client=ProbeRecordingLLM(SUFFICIENT)).handle_turn(
        CandidateTurnInput(session_id="sess-voice", turn_index=3, text_content=LONG_ANSWER),
        _state(practice_tier="challenge", current_turn_in_question=1),
    )
    assert output.action == TurnAction.NEXT_QUESTION


@pytest.mark.asyncio
async def test_challenge_still_probes_a_gap_as_a_gap() -> None:
    insufficient = {**SUFFICIENT, "sufficiency_status": "INSUFFICIENT", "missing_aspect": "invalidation"}
    output = await InterviewCoreEngine(llm_client=ProbeRecordingLLM(insufficient)).handle_turn(
        CandidateTurnInput(session_id="sess-voice", turn_index=3, text_content=LONG_ANSWER),
        _state(practice_tier="challenge"),
    )
    assert output.action == TurnAction.PROBE
    assert output.metadata["probe_kind"] == "gap"


def test_session_state_reads_the_tier_frozen_on_the_turn() -> None:
    session_row = {"id": "s1", "duration_minutes": 25, "locale": "vi-VN", "metadata": {}}
    turn = {
        "id": "t1",
        "status": "ASKED",
        "question_version_id": None,
        "question_snapshot": {"stage": InterviewStage.DEEP_DIVE.value, "questionText": "Q?", "practiceTier": "challenge"},
    }
    state = build_session_state_from_db(session_row, [turn], turn, False)
    assert state["practice_tier"] == "challenge"
    turn["question_snapshot"].pop("practiceTier")
    assert build_session_state_from_db(session_row, [turn], turn, False)["practice_tier"] == "standard"
