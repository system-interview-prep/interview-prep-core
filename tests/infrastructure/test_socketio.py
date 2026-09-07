from src.infrastructure.socketio import sio


def test_socketio_is_configured_for_asgi() -> None:
    assert sio.async_mode == "asgi"
