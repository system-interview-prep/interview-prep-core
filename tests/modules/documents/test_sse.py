import json

from src.modules.documents.sse import SSE_HEADERS, encode_sse, status_event_stream


class ConnectedRequest:
    async def is_disconnected(self) -> bool:
        return False


async def test_status_stream_emits_deltas_then_closes_at_terminal_state() -> None:
    states = iter(
        [
            {"cvId": "cv-1", "status": "PENDING", "error": None, "updatedAt": "t1"},
            {"cvId": "cv-1", "status": "DONE", "error": None, "updatedAt": "t2"},
        ]
    )

    async def load_status():
        return next(states)

    frames = [
        item
        async for item in status_event_stream(
            ConnectedRequest(), load_status, poll_interval_seconds=0, heartbeat_seconds=60
        )
    ]

    assert frames[0].startswith("event: status\n")
    assert json.loads(frames[0].split("data: ", 1)[1]) == {
        "cvId": "cv-1", "status": "PENDING", "error": None, "updatedAt": "t1"
    }
    assert frames[1].startswith("event: status\n")
    assert json.loads(frames[1].split("data: ", 1)[1]) == {
        "cvId": "cv-1", "status": "DONE", "error": None, "updatedAt": "t2"
    }
    assert json.loads(frames[2].split("data: ", 1)[1]) == {"status": "DONE"}


async def test_status_stream_returns_safe_error_when_snapshot_load_fails() -> None:
    async def unavailable():
        raise RuntimeError("database connection details must not reach the browser")

    frames = [
        item
        async for item in status_event_stream(
            ConnectedRequest(), unavailable, poll_interval_seconds=0, heartbeat_seconds=60
        )
    ]
    assert json.loads(frames[0].split("data: ", 1)[1]) == {"code": "status_unavailable"}


def test_sse_encoding_and_headers_are_proxy_safe() -> None:
    assert encode_sse(comment="keep-alive") == ": keep-alive\n\n"
    assert SSE_HEADERS["X-Accel-Buffering"] == "no"
