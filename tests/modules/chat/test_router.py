import pytest
from pydantic import ValidationError

from src.modules.chat.router import ChatRequest


def test_chat_request_validation() -> None:
    assert ChatRequest(sessionId="s1", prompt="Hello").language == "English"
    with pytest.raises(ValidationError):
        ChatRequest(sessionId="", prompt="")


def test_chat_routes_require_authentication(client) -> None:
    assert client.post("/ai/chat", json={"sessionId": "s1", "prompt": "Hello"}).status_code == 401
    assert client.get("/ai/history", params={"sessionId": "s1"}).status_code == 401
