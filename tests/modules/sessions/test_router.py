from datetime import UTC, datetime

import pytest

from src.modules.interviews.api.session_router import CreateSession, _session


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


async def test_legacy_close_refuses_open_structured_session() -> None:
    """M3: a CV+JD session must end through the runtime, which records end_reason."""
    from unittest.mock import AsyncMock, MagicMock

    import pytest
    from fastapi import HTTPException

    from src.modules.interviews.api.session_router import close_session

    db = AsyncMock()

    def _result(scalar=None, mapping=None):
        result = MagicMock()
        result.scalar_one_or_none.return_value = scalar
        result.mappings.return_value.one.return_value = mapping
        return result

    db.execute.side_effect = [
        _result(scalar=1),  # ownership check
        _result(scalar=None),  # UPDATE ... AND resume_id IS NULL matched nothing
        _result(mapping={"status": "OPEN", "ended_at": None}),
    ]
    with pytest.raises(HTTPException) as excinfo:
        await close_session("sess-structured", user={"sub": "u1"}, db=db)
    assert excinfo.value.status_code == 409
    db.commit.assert_not_called()
