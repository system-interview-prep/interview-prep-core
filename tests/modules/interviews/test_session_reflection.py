"""Candidate self-reflection on a finished session (debrief)."""

import importlib
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

interview_router = importlib.import_module("src.modules.interviews.api.router")

USER = {"sub": "user-a", "roles": []}


class _Db:
    def __init__(self, turn_ids: list[str], stored: dict | None = None) -> None:
        self.turn_ids = turn_ids
        self.stored = stored
        self.writes: list[dict] = []
        self.committed = False

    async def execute(self, statement, params):
        sql = str(statement)
        if "FROM interview_turns" in sql:
            rows = [(turn_id,) for turn_id in self.turn_ids]
            return SimpleNamespace(all=lambda: rows)
        if sql.startswith("INSERT INTO interview_session_reflections"):
            self.writes.append(params)
            row = {"self_ratings": params["ratings"], "takeaway": params["takeaway"], "updated_at": datetime.now(UTC)}
            return SimpleNamespace(mappings=lambda: SimpleNamespace(one=lambda: row))
        stored = self.stored
        return SimpleNamespace(mappings=lambda: SimpleNamespace(one_or_none=lambda: stored))

    async def commit(self):
        self.committed = True


@pytest.fixture(autouse=True)
def _owned(monkeypatch: pytest.MonkeyPatch) -> None:
    async def owned(_db, _uid, sid):
        return {"id": sid}

    monkeypatch.setattr(interview_router, "_owned_session", owned)


async def test_reflection_is_empty_until_the_candidate_submits() -> None:
    body = await interview_router.get_session_reflection("ses-1", user=USER, db=_Db([]))
    assert body == {"sessionId": "ses-1", "selfRatings": {}, "takeaway": None, "submitted": False, "updatedAt": None}


async def test_saving_stores_ratings_and_trimmed_takeaway() -> None:
    db = _Db(["turn-1", "turn-2"])
    payload = interview_router.SaveSessionReflection(selfRatings={"turn-1": 3, "turn-2": 1}, takeaway="  Nói số liệu trước.  ")

    body = await interview_router.save_session_reflection("ses-1", payload, user=USER, db=db)

    assert db.committed
    assert db.writes[0]["uid"] == "user-a"
    assert db.writes[0]["takeaway"] == "Nói số liệu trước."
    assert body["submitted"] is True


@pytest.mark.parametrize("ratings", [{"turn-1": 0}, {"turn-1": 5}])
async def test_ratings_outside_the_four_levels_are_rejected(ratings) -> None:
    payload = interview_router.SaveSessionReflection(selfRatings=ratings)
    with pytest.raises(HTTPException) as exc:
        await interview_router.save_session_reflection("ses-1", payload, user=USER, db=_Db(["turn-1"]))
    assert exc.value.status_code == 422


async def test_ratings_for_turns_of_another_session_are_rejected() -> None:
    payload = interview_router.SaveSessionReflection(selfRatings={"turn-of-other-session": 2})
    db = _Db(["turn-1"])
    with pytest.raises(HTTPException) as exc:
        await interview_router.save_session_reflection("ses-1", payload, user=USER, db=db)
    assert exc.value.status_code == 422
    assert db.writes == []
