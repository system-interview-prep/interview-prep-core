import importlib
from unittest.mock import AsyncMock, MagicMock

import pytest

# The package re-exports the APIRouter as `router`; load the module itself.
interviews_router = importlib.import_module("src.modules.interviews.api.router")



def _session(status="OPEN"):
    return {"id": "s1", "mode": "text", "experience_type": "interview_chat", "status": status}


@pytest.fixture
def owned(monkeypatch):
    holder = {"session": _session()}

    async def fake_owned(_db, _uid, _sid):
        return holder["session"]

    monkeypatch.setattr(interviews_router, "_owned_session", fake_owned)
    monkeypatch.setattr(interviews_router, "_require_runtime_mode", lambda *a, **k: None)
    return holder


@pytest.mark.asyncio
async def test_heartbeat_clears_the_left_mark_and_says_since_when_they_were_away(owned) -> None:
    db = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = "2026-10-20T02:00:00+00:00"
    db.execute.return_value = result

    response = await interviews_router.chat_presence(
        "s1", interviews_router.ChatPresence(state="here"), {"sub": "u1"}, db
    )

    assert response == {"open": True, "awaySince": "2026-10-20T02:00:00+00:00", "graceMinutes": 5}
    sql = str(db.execute.await_args.args[0])
    assert "- 'candidate_left_at'" in sql and "candidate_seen_at" in sql
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_left_marks_the_session_once(owned) -> None:
    db = AsyncMock()
    response = await interviews_router.chat_presence(
        "s1", interviews_router.ChatPresence(state="left"), {"sub": "u1"}, db
    )
    assert response["awaySince"] is None
    sql = str(db.execute.await_args.args[0])
    assert "'{candidate_left_at}'" in sql
    assert "metadata->>'candidate_left_at' IS NULL" in sql


@pytest.mark.asyncio
async def test_closed_session_is_left_alone(owned) -> None:
    owned["session"] = _session("CLOSED")
    db = AsyncMock()
    response = await interviews_router.chat_presence(
        "s1", interviews_router.ChatPresence(state="here"), {"sub": "u1"}, db
    )
    assert response["open"] is False
    db.execute.assert_not_awaited()
