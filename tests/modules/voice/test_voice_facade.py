"""The voice facade hands the right things to the command runner.

Found by the §13 pilot (2026-11-26): ``start_voice_interview`` referenced an
undefined name and every real voice session crashed at runtime start, while
``submit_voice_answer`` accepted the early-speech callback and dropped it.
Neither path had a test that reached the facade's own code.
"""

from __future__ import annotations

import pytest

from src.modules.interviews.session import voice_facade


@pytest.fixture
def runner(monkeypatch):
    seen: dict = {}

    def database_operations(*, db, user, session_row, on_early_speech=None):
        seen["on_early_speech"] = on_early_speech
        return {"ops": True}

    async def run_interview_command(*, session_id, command, operations, payload=None, event_id=None):
        seen.update({"session_id": session_id, "command": command, "operations": operations, "payload": payload, "event_id": event_id})
        return {"ok": True}

    monkeypatch.setattr(voice_facade, "database_operations", database_operations)
    monkeypatch.setattr(voice_facade, "run_interview_command", run_interview_command)
    return seen


@pytest.mark.asyncio
async def test_starting_a_voice_interview_opens_the_session_without_early_speech(runner) -> None:
    assert await voice_facade.start_voice_interview(object(), {"id": "s1"}) == {"ok": True}
    assert runner["command"] == "open" and runner["session_id"] == "s1"
    assert runner["on_early_speech"] is None


@pytest.mark.asyncio
async def test_submitting_an_answer_passes_the_callback_and_the_preview_key(runner) -> None:
    def speak(_text: str) -> None:
        pass

    await voice_facade.submit_voice_answer(
        object(), {"id": "s1"}, client_message_id="m-1", content="Dạ em dùng Redis.",
        precomputed_evaluation={"intent": "ANSWER"}, precomputed_decision={"action": "x"},
        preview_key={"session_id": "s1"}, on_early_speech=speak,
    )
    assert runner["command"] == "respond" and runner["event_id"] == "m-1"
    assert runner["on_early_speech"] is speak, "the Hybrid early line travels in the operations closure"
    payload = runner["payload"]
    assert payload["preview_key"] == {"session_id": "s1"}
    assert payload["precomputed_decision"] == {"action": "x"} and payload["modality"] == "VOICE"
    assert "on_early_speech" not in payload, "never in the checkpointed payload"
