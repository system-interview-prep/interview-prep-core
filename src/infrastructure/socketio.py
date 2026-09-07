import socketio

from src.core.config import get_settings

sio = socketio.AsyncServer(
    async_mode="asgi",
    cors_allowed_origins=get_settings().cors_origins,
    logger=False,
    engineio_logger=False,
)


async def emit_status(namespace: str, room: str, event: str, payload: dict) -> None:
    await sio.emit(event, payload, room=room, namespace=namespace)
