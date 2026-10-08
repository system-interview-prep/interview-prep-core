import pytest

from src.modules.interviews.adapters.video_calls.signaling import _room


def test_room_validation() -> None:
    assert _room({"roomId": " room-1 "}) == "room-1"
    with pytest.raises(ValueError):
        _room({})
