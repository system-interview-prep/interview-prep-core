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


class WarmupGreetingLLM:
    def __init__(self):
        self.system_context = ""
        self.probe_question = "Bạn đang học hay đã đi làm rồi?"

    async def generate_json(self, *, system_prompt: str, user_content: str):
        self.system_context = system_prompt
        return {
            "intent": "ANSWER",
            "sufficiency_status": "INSUFFICIENT",
            "is_sufficient": False,
            "missing_aspect": "self-introduction",
            "acknowledgement": "Chào bạn, mình bắt đầu nhé.",
            "probe_question": self.probe_question,
            "score": 1.0,
        }

    async def generate_text(self, *, system_prompt: str, user_content: str):
        raise AssertionError("Warm-up continuation should reuse the evaluation response")


class WarmupHistoryLLM(WarmupGreetingLLM):
    def __init__(self):
        super().__init__()
        self.evaluation_context = ""

    async def generate_json(self, *, system_prompt: str, user_content: str):
        self.evaluation_context = user_content
        return await super().generate_json(system_prompt=system_prompt, user_content=user_content)


class WarmupCompleteLLM:
    def __init__(self):
        self.transition_prompt = ""

    async def generate_json(self, *, system_prompt: str, user_content: str):
        return {
            "intent": "ANSWER",
            "sufficiency_status": "SUFFICIENT",
            "is_sufficient": True,
            "acknowledgement": "Mình ghi nhận rồi.",
            "score": 6.0,
        }

    async def generate_text(self, *, system_prompt: str, user_content: str):
        self.transition_prompt = user_content
        return "Ok, mình hiểu rồi. Tiếp theo mình hỏi bạn về dự án HamTech nhé."


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


@pytest.mark.asyncio
async def test_warmup_greeting_keeps_opening_turn_and_invites_candidate_to_continue() -> None:
    llm = WarmupGreetingLLM()
    engine = InterviewCoreEngine(llm_client=llm)
    opening_question = "Bạn giới thiệu ngắn về bản thân trong một, hai phút nhé?"

    result = await engine.handle_turn(
        CandidateTurnInput(session_id="session-1", turn_index=0, text_content="Dạ em chào anh ạ"),
        {
            "current_stage": InterviewStage.WARM_UP.value,
            "started_at": datetime.now(timezone.utc),
            "target_duration_minutes": 25,
            "questions_pool": {
                "VALIDATE": [
                    {
                        "question_id": "project-question-1",
                        "competency": "Xác thực CV & Dự án",
                        "stage": "VALIDATE",
                        "main_prompt": "Bạn trực tiếp đóng góp gì cho dự án?",
                    }
                ]
            },
            "current_question_context": {
                "question_id": "warmup-intro",
                "competency": "Giới thiệu & Khởi động",
                "main_prompt": opening_question,
            },
        },
    )

    assert result.action == TurnAction.CLARIFY
    assert result.current_stage == InterviewStage.WARM_UP
    assert result.metadata["warmup_continuation"] is True
    assert result.message_text == "Bạn đang học hay đã đi làm rồi?"
    assert result.metadata["speech_plan"] == [
        {"act": "QUESTION", "source": "llm_evaluation"}
    ]
    assert "HƯỚNG DẪN RIÊNG CHO PHẦN MỞ ĐẦU" in llm.system_context
    assert "Không hỏi lại" in llm.system_context


@pytest.mark.asyncio
async def test_warmup_continuation_uses_accumulated_intro_context() -> None:
    llm = WarmupHistoryLLM()
    llm.probe_question = "Bạn đang học năm mấy rồi?"
    engine = InterviewCoreEngine(llm_client=llm)
    earlier = "Em là Trường, đang học ngành Công nghệ thông tin ở IUH."
    latest = "Em cũng đang tìm cơ hội làm Full Stack Developer."
    accumulated_intro = f"{earlier}\n{latest}"

    result = await engine.handle_turn(
        CandidateTurnInput(session_id="session-1", turn_index=0, text_content=accumulated_intro),
        {
            "current_stage": InterviewStage.WARM_UP.value,
            "started_at": datetime.now(timezone.utc),
            "target_duration_minutes": 25,
            "questions_pool": {"VALIDATE": []},
            "current_question_context": {
                "question_id": "warmup-intro",
                "competency": "Giới thiệu & Khởi động",
                "main_prompt": "Bạn giới thiệu ngắn về bản thân, công việc hiện tại và lý do quan tâm vị trí này?",
            },
        },
    )

    assert result.metadata["warmup_continuation"] is True
    assert result.message_text.endswith("Bạn đang học năm mấy rồi?")
    assert earlier in llm.evaluation_context
    assert latest in llm.evaluation_context


@pytest.mark.asyncio
async def test_completed_warmup_announces_topic_without_asking_or_summarizing() -> None:
    llm = WarmupCompleteLLM()
    engine = InterviewCoreEngine(llm_client=llm)
    next_prompt = "Trong dự án HamTech, bạn trực tiếp phụ trách phần nào?"
    result = await engine.handle_turn(
        CandidateTurnInput(
            session_id="session-1",
            turn_index=0,
            text_content=(
                "Dạ em là sinh viên năm cuối ngành Kỹ thuật phần mềm tại IUH và em đang tìm cơ hội "
                "Full Stack Developer. Em thích thiết kế giao diện, đồng thời cũng quan tâm đến logic "
                "backend và cách các luồng dữ liệu được xử lý trong ứng dụng."
            ),
        ),
        {
            "current_stage": InterviewStage.WARM_UP.value,
            "started_at": datetime.now(timezone.utc),
            "target_duration_minutes": 25,
            "questions_pool": {
                "VALIDATE": [
                    {
                        "question_id": "project-question-1",
                        "competency": "Xác thực CV & Dự án",
                        "stage": "VALIDATE",
                        "main_prompt": next_prompt,
                    }
                ]
            },
            "current_question_context": {
                "question_id": "warmup-intro",
                "competency": "Giới thiệu & Khởi động",
                "main_prompt": "Giới thiệu ngắn về bản thân và lý do quan tâm vị trí này?",
            },
        },
    )

    assert result.action == TurnAction.NEXT_QUESTION
    assert result.metadata["awaiting_candidate_ack"] is True
    assert result.message_text == "Ok, mình hiểu rồi. Tiếp theo mình hỏi bạn về dự án HamTech nhé."
    assert "?" not in result.message_text
    assert next_prompt not in result.message_text
    assert "năm cuối" not in result.message_text
    assert "Không tóm tắt" in llm.transition_prompt
