from typing import Any

import jwt

from src.core.config import get_settings
from src.infrastructure.socketio import sio


def _room(data: Any) -> str:
    if not isinstance(data, dict):
        raise ValueError("Payload must be an object")
    room = str(data.get("roomId") or "").strip()
    if not room or len(room) > 128:
        raise ValueError("roomId is required")
    return room


@sio.event
async def connect(sid: str, environ: dict, auth: dict | None) -> bool:
    del environ
    token = str((auth or {}).get("token") or "").removeprefix("Bearer ").strip()
    try:
        settings = get_settings()
        claims = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
        await sio.save_session(sid, {"user_id": str(claims["sub"])})
        return True
    except (jwt.InvalidTokenError, KeyError):
        return False


@sio.on("join-room")
async def join_room(sid: str, data: dict) -> dict:
    room = _room(data)
    await sio.enter_room(sid, room)
    return {"roomId": room, "joined": True}


async def _relay(event: str, sid: str, data: dict) -> None:
    room = _room(data)
    await sio.emit(event, data, room=room, skip_sid=sid)


@sio.on("offer")
async def offer(sid: str, data: dict) -> None:
    await _relay("offer", sid, data)


@sio.on("answer")
async def answer(sid: str, data: dict) -> None:
    await _relay("answer", sid, data)


@sio.on("ice-candidate")
async def ice_candidate(sid: str, data: dict) -> None:
    await _relay("ice-candidate", sid, data)


@sio.on("leave-room")
async def leave_room(sid: str, data: dict) -> None:
    await sio.leave_room(sid, _room(data))
