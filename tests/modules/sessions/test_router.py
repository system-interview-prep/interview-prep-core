from datetime import UTC, datetime

import pytest

from src.modules.sessions.router import CreateSession, _session


def test_session_contract() -> None:
    now = datetime.now(UTC)
    assert CreateSession(type="Voice").language == "English"
    assert (
        _session(
            {
                "id": "s1",
                "type": "Chat",
                "language": "English",
                "status": "OPEN",
                "started_at": now,
                "ended_at": None,
            }
        )["endedAt"]
        is None
    )


@pytest.mark.parametrize(
    ("method", "path", "kwargs"),
    [("get", "/ai/sessions", {}), ("post", "/ai/session", {"json": {"type": "Chat"}})],
)
def test_session_routes_require_authentication(client, method: str, path: str, kwargs: dict) -> None:
    assert getattr(client, method)(path, **kwargs).status_code == 401
