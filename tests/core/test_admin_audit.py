"""Admin action log: written after the action, never failing it."""

import json
from types import SimpleNamespace

from src.core.admin_audit import DETAIL_MAX_CHARS, record_admin_action


class _Db:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.params: list[dict] = []
        self.commits = 0
        self.rollbacks = 0

    async def execute(self, statement, params):
        if self.fail:
            raise RuntimeError('relation "admin_access_logs" does not exist')
        self.params.append(params)
        return SimpleNamespace()

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


ADMIN = {"sub": "admin-1", "email": "admin@intervia.io"}


async def test_row_carries_who_what_and_target() -> None:
    db = _Db()
    await record_admin_action(db, ADMIN, "PUBLISH_JD", target_id="jd-1", detail={"versionId": "v2"})
    (row,) = db.params
    assert row["uid"] == "admin-1" and row["email"] == "admin@intervia.io"
    assert row["action"] == "PUBLISH_JD" and row["tid"] == "jd-1" and row["sid"] is None
    assert json.loads(row["detail"]) == {"versionId": "v2"}
    assert db.commits == 1


async def test_large_detail_is_replaced_not_stored() -> None:
    db = _Db()
    await record_admin_action(db, ADMIN, "EDIT_JD", detail={"text": "x" * (DETAIL_MAX_CHARS + 10)})
    assert json.loads(db.params[0]["detail"]) == {"truncated": True}


async def test_a_missing_table_does_not_raise() -> None:
    db = _Db(fail=True)
    await record_admin_action(db, ADMIN, "APPROVE_QUESTION", target_id="q-1")
    assert db.rollbacks == 1 and db.commits == 0
