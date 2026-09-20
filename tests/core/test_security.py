from datetime import UTC, datetime
from unittest.mock import AsyncMock

import jwt
import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from src.core.config import get_settings
from src.core.security import (
    create_access_token,
    current_user,
    hash_password,
    require_admin,
    require_roles,
    verify_password,
)


@pytest.mark.asyncio
async def test_schema_bootstrap_releases_advisory_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    from src import main

    events: list[str] = []

    class Connection:
        async def execute(self, statement, _: dict) -> None:
            events.append(statement.text)

    class ConnectionContext:
        async def __aenter__(self) -> Connection:
            return Connection()

        async def __aexit__(self, *_: object) -> None:
            return None

    class Engine:
        def connect(self) -> ConnectionContext:
            return ConnectionContext()

    async def taxonomy(_: object) -> None:
        events.append("taxonomy")

    async def question_bank(_: object) -> None:
        events.append("question_bank")

    monkeypatch.setattr("src.modules.taxonomy.schema.create_taxonomy_schema", taxonomy)
    monkeypatch.setattr("src.modules.question_bank.schema.create_question_bank_schema", question_bank)
    await main.bootstrap_question_bank_schema(Engine())

    assert events == [
        "SELECT pg_advisory_lock(:key)",
        "taxonomy",
        "question_bank",
        "SELECT pg_advisory_unlock(:key)",
    ]


class _Mappings:
    def __init__(self, row: dict | None) -> None:
        self.row = row

    def one_or_none(self) -> dict | None:
        return self.row


class _RoleResult:
    def __init__(self, row: dict | None) -> None:
        self.row = row

    def mappings(self) -> _Mappings:
        return _Mappings(self.row)


@pytest.mark.asyncio
async def test_require_admin_accepts_only_admin() -> None:
    assert (await require_admin({"sub": "u1", "roles": ["ADMIN"]}))["sub"] == "u1"
    with pytest.raises(HTTPException) as error:
        await require_admin({"sub": "u1", "roles": ["CANDIDATE"]})
    assert error.value.status_code == 403


@pytest.mark.asyncio
async def test_actor_guard_allows_assigned_actor_and_admin() -> None:
    guard = require_roles("QUESTION_AUTHOR")
    assert (await guard({"sub": "author", "roles": ["QUESTION_AUTHOR"]}))["sub"] == "author"
    assert (await guard({"sub": "admin", "roles": ["ADMIN"]}))["sub"] == "admin"
    with pytest.raises(HTTPException):
        await guard({"sub": "candidate", "roles": ["CANDIDATE"]})


@pytest.mark.asyncio
async def test_actor_guard_uses_any_assigned_role() -> None:
    guard = require_roles("QUESTION_REVIEWER")
    assert (await guard({"sub": "u1", "roles": ["CANDIDATE", "QUESTION_REVIEWER"]}))["sub"] == "u1"


def test_bcrypt_hash_and_verification() -> None:
    password_hash = hash_password("Strong123")
    assert password_hash != "Strong123"
    assert password_hash.startswith("$2")
    assert verify_password("Strong123", password_hash) is True
    assert verify_password("Wrong123", password_hash) is False
    assert verify_password("Strong123", None) is False


def test_access_token_contains_required_claims_and_configured_expiry() -> None:
    settings = get_settings()
    token = create_access_token("user-1", "user@example.com", ["CANDIDATE"])
    payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])

    assert payload["sub"] == payload["userId"] == "user-1"
    assert payload["email"] == "user@example.com"
    assert payload["roles"] == ["CANDIDATE"]
    lifetime = datetime.fromtimestamp(payload["exp"], UTC) - datetime.fromtimestamp(payload["iat"], UTC)
    assert lifetime.total_seconds() == pytest.approx(settings.jwt_expires_minutes * 60, abs=1)


@pytest.mark.asyncio
async def test_current_user_uses_current_database_roles() -> None:
    token = create_access_token("user-1", "user@example.com", ["ADMIN"])
    db = AsyncMock()
    db.execute.return_value = _RoleResult({"is_active": True, "roles": ["CANDIDATE"]})
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)

    user = await current_user(credentials, db)

    assert user == {"sub": "user-1", "email": "user@example.com", "roles": ["CANDIDATE"]}


@pytest.mark.asyncio
async def test_current_user_rejects_inactive_database_user() -> None:
    token = create_access_token("user-1", "user@example.com", ["ADMIN"])
    db = AsyncMock()
    db.execute.return_value = _RoleResult({"is_active": False, "roles": ["ADMIN"]})
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)

    with pytest.raises(HTTPException) as error:
        await current_user(credentials, db)

    assert error.value.status_code == 401
