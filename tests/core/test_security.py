from datetime import UTC, datetime

import jwt
import pytest
from fastapi import HTTPException

from src.core.config import get_settings
from src.core.security import create_access_token, hash_password, require_admin, require_roles, verify_password


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
