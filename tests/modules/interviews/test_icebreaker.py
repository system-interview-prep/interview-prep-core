"""Getting acquainted before the warm-up, in the model's own words (user request, 2026-11-26).

The voice interview used to open cold: the fixed greeting was stored but the
voice start speaks only the last assistant message, the warm-up question.
Now the session opens with a model-written greeting and one small-talk
question; a short reply gets a model-written reaction followed by the frozen
warm-up question (no Core call); a long reply is the introduction itself.
"""

from __future__ import annotations

from typing import Any

import pytest

from src.modules.interviews.session import chat_runtime
from src.modules.interviews.session.chat_runtime import process_candidate_message, start_chat_session
from tests.modules.interviews.test_chat_runtime import MockChatSession

WARM_UP = "Bạn giới thiệu ngắn về bản thân trong một, hai phút: bạn đang làm gì và vì sao quan tâm vị trí này?"
OPENING = (
    "Chào Năng, mình là người phỏng vấn AI của Intervia. Hôm nay mình cùng luyện phỏng vấn vị trí lập trình viên "
    "C#, .NET trong khoảng mười lăm phút nhé. Trước khi bắt đầu, hôm nay bạn thấy thế nào, có hồi hộp không?"
)
REACTION = "Hồi hộp một chút là bình thường mà, mình cứ trò chuyện thoải mái thôi."


def _session() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    row = {
        "id": "sess-ice", "user_id": "usr-1", "job_id": "job-1", "resume_id": "cv-1", "mode": "voice",
        "experience_type": "voice_interview", "end_reason": None, "locale": "vi-VN", "status": "OPEN",
        "plan_status": "LOCKED", "duration_minutes": 15,
    }
    turns = [
        {"id": "turn-0", "session_id": "sess-ice", "turn_index": 0, "status": "PLANNED", "question_version_id": None,
         "question_snapshot": {"stage": "WARM_UP", "questionText": WARM_UP}, "answer_text": None},
        {"id": "turn-1", "session_id": "sess-ice", "turn_index": 1, "status": "PLANNED", "question_version_id": "q1",
         "question_snapshot": {"stage": "VALIDATE", "questionText": "Bạn đóng góp gì cho dự án?"}, "answer_text": None},
    ]
    return row, turns


class ScriptedSpeech:
    """Stands in for the engine's line generator; applies the caller's guard like the real one."""

    def __init__(self, **lines: str):
        self.lines = lines
        self.calls: list[dict[str, Any]] = []

    async def _generate_interviewer_line(self, *, purpose, context, is_vi, reject=None, **_kw):
        self.calls.append({"purpose": purpose, "context": context})
        line = self.lines.get(purpose)
        if line is None:
            raise RuntimeError("model down")
        if reject and reject(line):
            raise ValueError("rejected")
        return line


@pytest.fixture
def speech(monkeypatch):
    def install(**lines: str) -> ScriptedSpeech:
        fake = ScriptedSpeech(**lines)
        monkeypatch.setattr(chat_runtime, "_speech_engine", lambda _row: fake)
        return fake

    return install


@pytest.mark.asyncio
async def test_the_session_opens_with_the_models_greeting_and_waits(speech) -> None:
    fake = speech(opening_icebreaker=OPENING)
    row, turns = _session()
    db = MockChatSession(row, turns)
    db.candidate_name = "Nguyễn Văn Năng"
    await start_chat_session(db, row)

    assistant = [m for m in db.messages if m["role"] == "assistant"]
    assert [m["content"] for m in assistant] == [OPENING]
    assert assistant[0]["metadata"]["awaitingIcebreaker"] is True
    # The voice start speaks the last assistant message: now the greeting.
    assert assistant[-1]["content"] == OPENING
    context = fake.calls[0]["context"]
    assert "Chưa hỏi ứng viên giới thiệu bản thân" in context and "15 phút" in context


@pytest.mark.asyncio
async def test_a_short_reply_gets_a_reaction_and_then_the_warm_up_question(speech) -> None:
    fake = speech(opening_icebreaker=OPENING, icebreaker_reply=REACTION)
    row, turns = _session()
    db = MockChatSession(row, turns)
    await start_chat_session(db, row)

    result = await process_candidate_message(db, row, client_message_id="m1", content="Dạ em khỏe ạ, hơi hồi hộp chút.")

    assert result["action"] == "ASK_MAIN"
    assert result["message"] == f"{REACTION}\n\n{WARM_UP}"
    reply = [m for m in db.messages if m["role"] == "user"][-1]
    # Stored as ACKNOWLEDGMENT by a SQL literal the fake database does not parse;
    # the purpose is what marks it as small talk rather than an answer.
    assert reply["metadata"]["purpose"] == "icebreaker_reply"
    question = [m for m in db.messages if m["role"] == "assistant"][-1]
    assert question["message_type"] == "MAIN_QUESTION" and question["metadata"]["icebreakerConsumed"] is True
    assert db.turns["turn-0"]["status"] == "ASKED", "small talk is not the warm-up answer"
    assert "Dạ em khỏe ạ" in fake.calls[-1]["context"]


@pytest.mark.asyncio
async def test_without_a_reaction_the_question_is_still_asked(speech) -> None:
    speech(opening_icebreaker=OPENING)  # no icebreaker_reply line: the model fails
    row, turns = _session()
    db = MockChatSession(row, turns)
    await start_chat_session(db, row)
    result = await process_candidate_message(db, row, client_message_id="m1", content="Dạ em nghe rõ ạ.")
    assert result["message"] == WARM_UP


@pytest.mark.asyncio
async def test_a_failed_greeting_falls_back_to_the_fixed_opening_and_question(speech) -> None:
    speech()  # the model fails
    row, turns = _session()
    db = MockChatSession(row, turns)
    await start_chat_session(db, row)
    assistant = [m for m in db.messages if m["role"] == "assistant"]
    assert len(assistant) == 2 and assistant[-1]["content"] == WARM_UP
    assert "awaitingIcebreaker" not in assistant[0]["metadata"]


@pytest.mark.asyncio
async def test_the_icebreaker_can_be_switched_off(speech, monkeypatch) -> None:
    fake = speech(opening_icebreaker=OPENING)
    monkeypatch.setenv("INTERVIEW_ICEBREAKER_ENABLED", "0")
    row, turns = _session()
    db = MockChatSession(row, turns)
    await start_chat_session(db, row)
    assert fake.calls == []
    assert [m for m in db.messages if m["role"] == "assistant"][-1]["content"] == WARM_UP


def test_the_greeting_must_end_in_small_talk_not_the_warm_up() -> None:
    reject = chat_runtime._icebreaker_opening_rejection
    assert reject(OPENING) is None
    assert reject("Chào Năng, mình là người phỏng vấn AI của Intervia.") == "no_question"
    assert reject("Chào Năng! Bạn giới thiệu ngắn về bản thân được không?") == "asks_the_warm_up"


def test_the_reaction_never_asks() -> None:
    reject = chat_runtime._icebreaker_reply_rejection
    assert reject(REACTION) is None
    assert reject("Vậy à, bạn học trường nào?") == "is_question"


@pytest.mark.asyncio
async def test_a_long_reply_is_the_introduction_and_goes_to_core() -> None:
    """Someone who answers the small talk by introducing themselves has answered
    the warm-up: no icebreaker reaction, Core takes it as the answer."""
    long_reply = " ".join(["Dạ em khỏe ạ, em là sinh viên năm cuối ngành kỹ thuật phần mềm"] * 4)
    row, turns = _session()
    ctx = chat_runtime.TurnContext(
        session_id="sess-ice", session_row=row, turns=turns, current_turn=turns[0],
        last_asst_msg={"content": OPENING, "metadata": {"awaitingIcebreaker": True}},
        cleaned_content=long_reply, elapsed_override_seconds=None,
    )
    assert await chat_runtime._icebreaker_lead(ctx) is None
    stop = chat_runtime.TurnContext(
        session_id="sess-ice", session_row=row, turns=turns, current_turn=turns[0],
        last_asst_msg={"content": OPENING, "metadata": {"awaitingIcebreaker": True}},
        cleaned_content="Em muốn dừng phỏng vấn ạ.", elapsed_override_seconds=None,
    )
    assert await chat_runtime._icebreaker_lead(stop) is None
