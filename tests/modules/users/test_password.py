"""Changing one's own password."""

import importlib
from types import SimpleNamespace

import pytest

from src.core.security import hash_password, verify_password

module = importlib.import_module("src.modules.users.password")


class _Db:
    def __init__(self, password_hash):
        self.row = None if password_hash == "missing" else {"password_hash": password_hash}
        self.updates: list[dict] = []
        self.commits = 0

    async def execute(self, statement, params):
        if str(statement).startswith("UPDATE"):
            self.updates.append(params)
            return SimpleNamespace()
        return SimpleNamespace(mappings=lambda: SimpleNamespace(one_or_none=lambda: self.row))

    async def commit(self):
        self.commits += 1


OLD = "Old-pass-123"


@pytest.mark.parametrize(
    ("stored", "current", "new", "code"),
    [
        ("missing", OLD, "New-pass-456", "not_found"),
        (None, OLD, "New-pass-456", "no_password"),
        ("hash", "wrong", "New-pass-456", "wrong_password"),
        ("hash", OLD, "short", "too_short"),
        ("hash", OLD, OLD, "same_password"),
    ],
)
async def test_refusals(stored, current, new, code) -> None:
    db = _Db(hash_password(OLD) if stored == "hash" else stored)
    with pytest.raises(module.PasswordChangeError) as refused:
        await module.change_password(db, "u1", current, new)
    assert refused.value.code == code
    assert db.updates == []


async def test_new_password_is_stored_hashed() -> None:
    db = _Db(hash_password(OLD))
    await module.change_password(db, "u1", OLD, "New-pass-456")
    (update,) = db.updates
    assert update["id"] == "u1" and update["hash"] != "New-pass-456"
    assert verify_password("New-pass-456", update["hash"])
    assert db.commits == 1
