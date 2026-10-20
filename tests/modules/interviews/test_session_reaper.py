from unittest.mock import AsyncMock, MagicMock

import pytest

from src.modules.interviews.application.session_reaper import reap_abandoned_sessions


class _SessionContext:
    def __init__(self, db: AsyncMock) -> None:
        self.db = db

    async def __aenter__(self) -> AsyncMock:
        return self.db

    async def __aexit__(self, *_args: object) -> None:
        return None


@pytest.mark.asyncio
async def test_reaper_closes_expired_and_abandoned_sessions() -> None:
    db = AsyncMock()
    result = MagicMock()
    result.all.return_value = [("expired-1",), ("abandoned-1",)]
    db.execute.return_value = result

    count = await reap_abandoned_sessions(lambda: _SessionContext(db))  # type: ignore[arg-type]

    assert count == 2
    sql = str(db.execute.await_args.args[0])
    assert "started_at + make_interval" in sql
    assert "updated_at < now() - interval '2 hours'" in sql
    assert "created_at" not in sql
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_reaper_closes_rooms_the_candidate_left_as_candidate_left() -> None:
    db = AsyncMock()
    result = MagicMock()
    result.all.return_value = []
    db.execute.return_value = result

    await reap_abandoned_sessions(lambda: _SessionContext(db))  # type: ignore[arg-type]

    sql = str(db.execute.await_args.args[0])
    params = db.execute.await_args.args[1]
    assert "THEN 'CANDIDATE_LEFT'" in sql
    assert "metadata->>'candidate_left_at'" in sql
    # Chat rooms: no heartbeat for the grace period also counts as having left.
    assert "mode = 'text' AND metadata->>'candidate_seen_at'" in sql
    assert params == {"left_grace": 5}
