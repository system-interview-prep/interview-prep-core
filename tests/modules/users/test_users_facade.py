"""The account's name, as other modules read it (product owner's call, 2026-11-27)."""

from __future__ import annotations

import pytest

from src.modules.users.facade import account_display_name


class _Db:
    def __init__(self, value):
        self.value = value
        self.calls: list[tuple[str, dict]] = []

    async def scalar(self, statement, params=None):
        self.calls.append((str(statement), dict(params or {})))
        return self.value


@pytest.mark.asyncio
async def test_the_account_name_is_read_by_user_id_and_trimmed() -> None:
    db = _Db("  Phi Trường ")
    assert await account_display_name(db, "usr-1") == "Phi Trường"
    sql, params = db.calls[0]
    assert "FROM users WHERE id = :user_id" in sql and params == {"user_id": "usr-1"}


@pytest.mark.asyncio
async def test_no_user_or_no_name_is_none() -> None:
    assert await account_display_name(_Db("x"), None) is None
    assert await account_display_name(_Db(None), "usr-1") is None
    assert await account_display_name(_Db("   "), "usr-1") is None
