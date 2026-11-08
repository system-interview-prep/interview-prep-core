from datetime import datetime, timezone

import pytest

from src.modules.interviews.core.contracts.interview import CandidateTurnInput, InterviewStage, TurnAction
from src.modules.interviews.core.conversation.turn_engine import InterviewCoreEngine


class InsufficientAnswerLLM:
    async def generate_json(self, *, system_prompt: str, user_content: str):
        return {
            "intent": "ANSWER",
            "sufficiency_status": "INSUFFICIENT",
            "is_sufficient": False,
            "missing_aspect": "candidate's concrete contribution",
            "acknowledgement": "Mình ghi nhận tên dự án rồi.",
            "probe_question": "Bạn đã trực tiếp làm phần nào trong HamTech?",
            "score": 3.0,
        }


class OverlongLineLLM:
    async def generate_text(self, *, system_prompt: str, user_content: str):
        return "Được rồi, mình ghi nhận dự án HamTech bạn vừa nhắc đến. Bây giờ mình muốn chuyển sang hỏi thêm về cách bạn thiết kế API và xử lý dữ liệu trong ứng dụng đó."


class TransitionCapturingEngine(InterviewCoreEngine):
    async def _transition_for(self, *args, **kwargs):
        self.transition_call = kwargs
        return "Được rồi, mình ghi nhận HamTech. Giờ mình hỏi bạn câu khác nhé."


def _state(probe_count: int) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "current_stage": InterviewStage.VALIDATE.value,
        "started_at": now,
        "target_duration_minutes": 25,
        "remaining_time": 20 * 60,
        "current_turn_in_question": probe_count,
        "current_question_context": {
            "question_id": "project-question-1",
            "competency": "Xác thực CV & Dự án",
            "main_prompt": "Bạn từng trực tiếp đóng góp gì cho dự án này?",
        },
        "asked_question_ids": ["project-question-1"],
        "questions_pool": {
            "VALIDATE": [
                {
                    "question_id": "project-question-1",
                    "competency": "Xác thực CV & Dự án",
                    "stage": "VALIDATE",
                    "main_prompt": "Bạn từng trực tiếp đóng góp gì cho dự án này?",
                },
                {
                    "question_id": "next-question-1",
                    "competency": "Kỹ năng kỹ thuật",
                    "stage": "VALIDATE",
                    "main_prompt": "Bạn thiết kế API của ứng dụng đó như thế nào?",
                },
            ]
        },
    }


@pytest.mark.asyncio
async def test_insufficient_answer_after_first_probe_gets_one_more_followup() -> None:
    engine = InterviewCoreEngine(llm_client=InsufficientAnswerLLM())

    result = await engine.handle_turn(
        CandidateTurnInput(
            session_id="session-1",
            turn_index=3,
            text_content="Dự án HamTech ạ.",
        ),
        _state(probe_count=1),
    )

    assert result.action == TurnAction.PROBE
    assert result.metadata["turn_in_question"] == 2
    assert "trực tiếp làm phần nào" in result.message_text


@pytest.mark.asyncio
async def test_after_probe_budget_exhaustion_transition_closes_topic_and_names_answer() -> None:
    engine = TransitionCapturingEngine(llm_client=InsufficientAnswerLLM())
    candidate_answer = "Dự án HamTech ạ."

    result = await engine.handle_turn(
        CandidateTurnInput(session_id="session-1", turn_index=4, text_content=candidate_answer),
        _state(probe_count=2),
    )

    assert result.action == TurnAction.NEXT_QUESTION
    assert "Được rồi, mình ghi nhận HamTech" in result.message_text
    assert "Bạn thiết kế API" in result.message_text
    assert engine.transition_call["close_previous_thread"] is True
    assert engine.transition_call["candidate_answer"] == candidate_answer


@pytest.mark.asyncio
async def test_overlong_ai_transition_does_not_fail_the_chat_turn() -> None:
    engine = InterviewCoreEngine(llm_client=OverlongLineLLM())

    line = await engine._generate_interviewer_line(
        purpose="transition_to_next_topic",
        context="The candidate mentioned HamTech; move to a question about API design.",
        is_vi=True,
        max_words=20,
    )

    assert "HamTech" in line
    assert "xử lý dữ liệu" in line
