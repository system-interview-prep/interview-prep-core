import pytest
from pydantic import ValidationError

from src.modules.video_calls.router import StartCall


def test_video_call_model_validates_room() -> None:
    assert StartCall(roomId="room-1").sessionId is None
    with pytest.raises(ValidationError):
        StartCall(roomId="")


@pytest.mark.parametrize(("method", "path", "kwargs"), [("get", "/interview/video-calls", {}), ("post", "/interview/video-calls/start", {"json": {"roomId": "room-1"}})])
def test_video_call_routes_require_authentication(client, method: str, path: str, kwargs: dict) -> None:
    assert getattr(client, method)(path, **kwargs).status_code == 401
