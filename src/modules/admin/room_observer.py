"""Admin observer seat in a live interview room (LiveKit).

An admin joins ``interview-<session id>`` as a *hidden*, listen-only
participant: it can subscribe to the candidate's microphone/camera and the
AI interviewer's voice, and read the room's data and transcription streams,
but it cannot publish anything. Hidden participants are not listed to the
other participants, so the voice agent's "is the candidate still here"
check and the candidate's UI are unaffected. The token carries no agent
dispatch, so joining never starts a second interviewer.

Every seat issued is written to ``admin_access_logs`` (OBSERVE_INTERVIEW_ROOM);
no log, no token.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

OBSERVER_TTL = timedelta(minutes=10)
LIVE_MODES = frozenset({"voice", "video"})


def room_name(session_id: str) -> str:
    return f"interview-{session_id}"


def observer_identity(admin_id: str) -> str:
    return f"admin-observer-{admin_id}"


def observe_refusal(session: dict[str, Any] | None) -> tuple[int, str] | None:
    """(HTTP status, detail) when the room cannot be observed, else None."""
    if session is None:
        return 404, "Interview session not found"
    if session.get("mode") not in LIVE_MODES:
        return 409, "not_media_session"
    if session.get("status") != "OPEN":
        return 409, "session_not_open"
    return None


async def room_is_running(api: Any, *, url: str, key: str, secret: str, session_id: str) -> bool | None:
    """True when the room exists with someone in it; None when LiveKit cannot be asked.

    An observer must never be the one who creates the room: the room is
    configured (voice agent dispatch) by the candidate's token when the
    candidate creates it, so an admin arriving first would leave the
    interview without an interviewer.
    """
    try:
        client = api.LiveKitAPI(url, key, secret)
    except Exception:  # noqa: BLE001
        return None
    try:
        response = await client.room.list_rooms(api.ListRoomsRequest(names=[room_name(session_id)]))
        rooms = list(getattr(response, "rooms", []) or [])
        return any(int(getattr(room, "num_participants", 0) or 0) > 0 for room in rooms)
    except Exception:  # noqa: BLE001
        return None
    finally:
        try:
            await client.aclose()
        except Exception:  # noqa: BLE001
            pass


async def close_room(api: Any, *, url: str, key: str, secret: str, session_id: str) -> bool | None:
    """Delete the interview room, which disconnects the candidate and the voice agent.

    Used when an admin closes a session: without it the room stays up and the
    agent keeps talking into a session that is already CLOSED. True when a
    room was deleted, False when there was none, None when LiveKit cannot be
    asked. Never raises: closing the session must not depend on LiveKit.
    """
    try:
        client = api.LiveKitAPI(url, key, secret)
    except Exception:  # noqa: BLE001
        return None
    try:
        name = room_name(session_id)
        response = await client.room.list_rooms(api.ListRoomsRequest(names=[name]))
        if not list(getattr(response, "rooms", []) or []):
            return False
        await client.room.delete_room(api.DeleteRoomRequest(room=name))
        return True
    except Exception:  # noqa: BLE001
        return None
    finally:
        try:
            await client.aclose()
        except Exception:  # noqa: BLE001
            pass


def observer_grants(api: Any, session_id: str) -> Any:
    """Listen-only, hidden grants (``api`` is ``livekit.api``)."""
    return api.VideoGrants(
        room_join=True,
        room=room_name(session_id),
        can_publish=False,
        can_publish_data=False,
        can_subscribe=True,
        hidden=True,
    )


def observer_token(
    api: Any, *, key: str, secret: str, session_id: str, admin_id: str, admin_name: str | None
) -> str:
    return (
        api.AccessToken(key, secret)
        .with_identity(observer_identity(admin_id))
        .with_name(admin_name or "Admin observer")
        .with_ttl(OBSERVER_TTL)
        .with_grants(observer_grants(api, session_id))
        .to_jwt()
    )
