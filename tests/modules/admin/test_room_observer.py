import asyncio
from types import SimpleNamespace

import jwt
from livekit import api

from src.modules.admin import room_observer


def test_refuses_missing_text_and_closed_sessions() -> None:
    assert room_observer.observe_refusal(None) == (404, "Interview session not found")
    assert room_observer.observe_refusal({"mode": "text", "status": "OPEN"}) == (409, "not_media_session")
    assert room_observer.observe_refusal({"mode": "voice", "status": "CLOSED"}) == (409, "session_not_open")
    assert room_observer.observe_refusal({"mode": "video", "status": "OPEN"}) is None


def test_observer_token_is_hidden_listen_only_and_dispatches_no_agent() -> None:
    token = room_observer.observer_token(
        api,
        key="key",
        secret="secret-secret-secret-secret-123",
        session_id="s1",
        admin_id="a1",
        admin_name="Admin",
    )
    claims = jwt.decode(token, options={"verify_signature": False})
    video = claims["video"]
    assert claims["sub"] == "admin-observer-a1"
    assert video["room"] == "interview-s1"
    assert video["roomJoin"] is True
    assert video["hidden"] is True
    assert video["canSubscribe"] is True
    assert video.get("canPublish") is False
    assert video.get("canPublishData") is False
    assert "roomConfig" not in claims


class _FakeRooms:
    def __init__(self, rooms=None, error=None):
        self.rooms, self.error, self.asked = rooms or [], error, None

    async def list_rooms(self, request):
        self.asked = request
        if self.error:
            raise self.error
        return SimpleNamespace(rooms=self.rooms)


def _fake_api(rooms):
    class Client:
        def __init__(self, *args):
            self.room = rooms

        async def aclose(self):
            return None

    return SimpleNamespace(LiveKitAPI=Client, ListRoomsRequest=lambda names: SimpleNamespace(names=names))


def test_room_is_running_needs_an_existing_room_with_participants() -> None:
    def ask(rooms):
        return asyncio.run(
            room_observer.room_is_running(_fake_api(rooms), url="u", key="k", secret="s", session_id="s1")
        )

    rooms = _FakeRooms([SimpleNamespace(num_participants=2)])
    assert ask(rooms) is True
    assert rooms.asked.names == ["interview-s1"]
    assert ask(_FakeRooms([])) is False
    assert ask(_FakeRooms([SimpleNamespace(num_participants=0)])) is False
    assert ask(_FakeRooms(error=RuntimeError("down"))) is None


class _DeletableRooms(_FakeRooms):
    def __init__(self, rooms=None, error=None):
        super().__init__(rooms, error)
        self.deleted = None

    async def delete_room(self, request):
        self.deleted = request.room
        return None


def _deleting_api(rooms):
    fake = _fake_api(rooms)
    fake.DeleteRoomRequest = lambda room: SimpleNamespace(room=room)
    return fake


def _close(rooms):
    return asyncio.run(
        room_observer.close_room(_deleting_api(rooms), url="u", key="k", secret="s", session_id="s1")
    )


def test_close_room_deletes_the_session_room_and_never_raises() -> None:
    rooms = _DeletableRooms(rooms=[SimpleNamespace(num_participants=2)])
    assert _close(rooms) is True and rooms.deleted == "interview-s1"

    # Chat session or room already gone: nothing to delete.
    empty = _DeletableRooms()
    assert _close(empty) is False
    assert empty.deleted is None

    # LiveKit down: the session close still goes through.
    down = _DeletableRooms(error=RuntimeError("unreachable"))
    assert _close(down) is None
