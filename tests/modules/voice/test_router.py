import pytest
from pydantic import ValidationError

from src.modules.voice.router import VoiceChatRequest


def test_voice_request_validation() -> None:
    assert VoiceChatRequest(sessionId="s1", prompt="Hi").language == "English"
    with pytest.raises(ValidationError):
        VoiceChatRequest(sessionId="s1", prompt="")


def test_voice_route_requires_authentication(client) -> None:
    assert client.post("/ai/chat-voice", json={"sessionId": "s1", "prompt": "Hi"}).status_code == 401


@pytest.mark.parametrize("path", ["transcribe", "speak", "realtime-token"])
def test_voice_lab_requires_authentication(client, path: str) -> None:
    response = client.post(f"/ai/voice-lab/{path}")
    assert response.status_code == 401
