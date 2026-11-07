"""Voice turn flow tests around the LiveKit adapter/Core boundary."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from src.modules.interviews.adapters.voice import livekit_agent
from src.modules.interviews.adapters.voice.answer_window import AnswerWindow


class _Events:
    def __init__(self) -> None:
        self.items: list[tuple[str, dict]] = []

    def emit(self, event: str, **fields) -> None:
        self.items.append((event, fields))

    def state(self, _state: str, **_fields) -> None:
        pass


class _Participant:
    def __init__(self) -> None:
        self.published: list[dict] = []

    async def publish_data(self, payload: str, **_kwargs) -> None:
        self.published.append(json.loads(payload))


class _RuntimeResponse:
    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return {
            "action": "NEXT_QUESTION",
            "sessionStatus": "OPEN",
            "assistantResponse": {"content": "Câu hỏi tiếp theo là gì?"},
        }


class _RuntimeClient:
    def __init__(self, while_processing) -> None:
        self.while_processing = while_processing
        self.payloads: list[dict] = []

    async def post(self, _path: str, *, json: dict) -> _RuntimeResponse:
        self.payloads.append(json)
        await self.while_processing()
        return _RuntimeResponse()


def _agent(while_processing) -> tuple[livekit_agent.VoiceLabAgent, list[str]]:
    agent = livekit_agent.VoiceLabAgent.__new__(livekit_agent.VoiceLabAgent)
    participant = _Participant()
    agent._room = SimpleNamespace(local_participant=participant)
    agent._events = _Events()
    agent._runtime_client = _RuntimeClient(while_processing)
    agent._session_id = "session-1"
    agent._room_name = "room-1"
    agent._locale = "vi-VN"
    agent._stt_provider = "assemblyai"
    agent._enable_speculative_preview = False
    agent._answer = AnswerWindow()
    agent._answer.add("Em đã hoàn thành câu trả lời ban đầu.", message_id="answer-1")
    agent._answer_pause_nudged = False
    agent._answer_pause_nudge_task = None
    agent._reprompt_task = None
    agent._preview_task = None
    agent._preview_target = ""
    agent._preview_pending = ""
    agent._preview_text = ""
    agent._preview_token = None
    agent._core_in_flight = False
    agent._repeat_after_core = False
    agent._last_answer_metrics = {}
    agent._last_interruption_telemetry = {}
    agent._last_interview_speech_text = "Câu hỏi vừa rồi là gì?"
    agent._held_speech_text = ""
    agent._held_resume_text = ""
    agent._answer_pause_nudged = False
    agent._active_speech_handle = None
    agent._active_speech_text = ""
    agent._active_speech_kind = "question"
    spoken: list[str] = []

    def speak(text: str, **_kwargs):
        spoken.append(text)
        agent._last_interview_speech_text = text
        return SimpleNamespace(wait_for_playout=lambda: None)

    agent.speak = speak
    return agent, spoken


def _user_message(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        text_content=text,
        transcript_confidence=0.95,
        metrics={},
        id="late-fragment-1",
        content=[text],
    )


@pytest.mark.asyncio
async def test_speech_during_core_is_visible_and_never_submitted_as_the_next_answer(monkeypatch) -> None:
    monkeypatch.setattr(livekit_agent, "processing_filler_enabled", lambda: False)
    late_text = "Phần em muốn bổ sung là em thiết kế API xử lý idempotency."
    agent = None

    async def candidate_finishes_speaking_while_core_is_running() -> None:
        await agent.on_user_turn_completed(None, _user_message(late_text))

    agent, spoken = _agent(candidate_finishes_speaking_while_core_is_running)

    await agent._commit_answer()
    await asyncio.sleep(0)

    assert len(agent._runtime_client.payloads) == 1
    assert agent._runtime_client.payloads[0]["content"] == "Em đã hoàn thành câu trả lời ban đầu."
    assert agent._answer.is_empty
    assert any(
        item.get("type") == "voice.late_transcript" and item.get("text") == late_text
        for item in agent._room.local_participant.published
    )
    assert any("câu trả lời trước đã được gửi đi rồi" in line.lower() for line in spoken)


@pytest.mark.asyncio
async def test_not_heard_during_core_is_deferred_and_not_sent_as_an_answer(monkeypatch) -> None:
    monkeypatch.setattr(livekit_agent, "processing_filler_enabled", lambda: False)
    request = "Dạ em không nghe được gì hết vì bạn nói xong là em chưa nghe thấy câu hỏi"
    agent = None

    async def candidate_requests_repeat_while_core_is_running() -> None:
        await agent.on_user_turn_completed(None, _user_message(request))

    agent, spoken = _agent(candidate_requests_repeat_while_core_is_running)

    await agent._commit_answer()
    await asyncio.sleep(0)

    assert len(agent._runtime_client.payloads) == 1
    assert agent._runtime_client.payloads[0]["content"] == "Em đã hoàn thành câu trả lời ban đầu."
    assert agent._answer.is_empty
    assert agent._repeat_after_core is False
    assert any("yêu cầu nhắc lại" in line.lower() for line in spoken)
    assert not any(
        item.get("type") == "voice.late_transcript" for item in agent._room.local_participant.published
    )


@pytest.mark.parametrize(
    ("spoken", "expected_interim", "expected"),
    [
        ("Em đang làm bắt em Developer.", "Em đang làm bắt em Developer.", "Em đang làm backend Developer."),
        (
            "Nhà trường bắt em phải nộp giấy xác nhận.",
            "Nhà trường bắt em phải nộp giấy xác nhận.",
            "Nhà trường bắt em phải nộp giấy xác nhận.",
        ),
        (
            "Em dùng you state để cập nhật giao diện.",
            "Em dùng useState để cập nhật giao diện.",
            "Em dùng useState để cập nhật giao diện.",
        ),
    ],
)
@pytest.mark.parametrize("matching_preview", [True, False])
@pytest.mark.asyncio
async def test_correction_reaches_core_with_raw_speech_and_exact_preview_binding(
    monkeypatch,
    spoken: str,
    expected_interim: str,
    expected: str,
    matching_preview: bool,
) -> None:
    monkeypatch.setattr(livekit_agent, "processing_filler_enabled", lambda: False)

    async def no_late_speech() -> None:
        pass

    agent, _ = _agent(no_late_speech)
    agent._answer = AnswerWindow()
    agent._session_context = "Backend Developer"
    agent._last_interview_speech_text = "Bạn dùng useState thế nào?"
    agent._enable_speculative_preview = True
    previews: list[str] = []
    agent._schedule_preview = previews.append
    agent._schedule_answer_pause_nudge = lambda: None
    agent._schedule_commit = lambda _delay: None

    agent.on_interim_transcript(spoken)
    message = _user_message(spoken)
    await agent.on_user_turn_completed(None, message)

    assert previews == [expected_interim, expected]
    assert message.content == [expected]
    assert agent._answer.text == expected
    assert agent._answer.raw_fragments == [spoken]
    agent._preview_text = expected if matching_preview else "Bản tạm trước khi ứng viên nói xong."
    agent._preview_token = "signed-preview-token"

    await agent._commit_answer()
    payload = agent._runtime_client.payloads[0]
    assert payload["content"] == expected
    assert payload["telemetry"]["rawTranscript"] == spoken
    assert payload["previewToken"] == ("signed-preview-token" if matching_preview else None)
    assert agent._answer.is_empty
