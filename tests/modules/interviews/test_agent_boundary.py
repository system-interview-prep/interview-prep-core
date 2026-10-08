from unittest.mock import AsyncMock

import pytest

from src.modules.interviews.adapters import (
    from_chat_message,
    from_video_turn,
    from_voice_transcript,
)
from src.modules.interviews.agent import InterviewAgent
from src.modules.interviews.core.interview_engine import InterviewCoreEngine
from src.modules.interviews.core.interview_types import (
    InterviewStage,
    InterviewerTurnOutput,
    TurnAction,
)


def test_modality_adapters_share_one_agent_contract() -> None:
    chat = from_chat_message(session_id="s", turn_index=0, content="hello")
    voice = from_voice_transcript(session_id="s", turn_index=0, final_transcript="hello")
    video = from_video_turn(
        session_id="s",
        turn_index=0,
        final_transcript="hello",
        video_observations={"camera_available": True},
    )

    assert chat.modality == "CHAT"
    assert voice.modality == "VOICE"
    assert video.modality == "VIDEO"
    assert video.telemetry["video_observations"]["camera_available"] is True
    assert chat.text_content == voice.text_content == video.text_content == "hello"


@pytest.mark.asyncio
async def test_agent_delegates_decision_to_existing_core() -> None:
    core = InterviewCoreEngine()
    core.handle_turn = AsyncMock(
        return_value=InterviewerTurnOutput(
            session_id="s",
            turn_index=1,
            message_text="Next question",
            action=TurnAction.NEXT_QUESTION,
            current_stage=InterviewStage.DEEP_DIVE,
            current_competency="Python",
            time_remaining_seconds=300,
        )
    )
    agent = InterviewAgent(core_engine=core)

    request = from_voice_transcript(
        session_id="s",
        turn_index=0,
        final_transcript="My answer",
        duration_seconds=12,
    )
    response = await agent.handle_turn(request, {"current_stage": "DEEP_DIVE"})

    assert response.action == "NEXT_QUESTION"
    assert response.current_stage == "DEEP_DIVE"
    assert response.message_text == "Next question"
    core.handle_turn.assert_awaited_once()
    assert core.handle_turn.await_args.args[0].modality == "VOICE"
