import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from src.modules.auth.router import GoogleLoginRequest, LoginRequest, RegisterRequest, _public_user, login


def test_auth_models_validate_required_credentials() -> None:
    assert RegisterRequest(email="USER@EXAMPLE.COM", password="secret123").email == "USER@EXAMPLE.COM"
    assert LoginRequest(email="user@example.com", password="secret").password == "secret"
    assert GoogleLoginRequest(accessToken="token").accessToken == "token"
    with pytest.raises(ValidationError):
        RegisterRequest(email="no", password="123")
    with pytest.raises(ValidationError):
        GoogleLoginRequest(accessToken="")


def test_auth_public_user_excludes_sensitive_columns() -> None:
    row = {
        "id": "u1",
        "email": "u@example.com",
        "name": "User",
        "role": "CANDIDATE",
        "provider": "local",
        "password": "secret",
    }
    assert _public_user(row) == {
        "id": "u1",
        "email": "u@example.com",
        "name": "User",
        "role": "CANDIDATE",
        "provider": "local",
    }


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        ("/auth/register", {"email": "x", "password": "123"}),
        ("/auth/login", {"email": "x"}),
        ("/auth/google", {"accessToken": ""}),
    ],
)
def test_auth_routes_reject_invalid_payloads(client, path: str, payload: dict) -> None:
    assert client.post(path, json=payload).status_code == 422


@pytest.mark.asyncio
async def test_password_login_rejects_google_only_account() -> None:
    class Result:
        def mappings(self):
            return self

        def one_or_none(self):
            return {"id": "u1", "email": "u@example.com", "password": None, "name": "U",
                    "role": "CANDIDATE", "provider": "google"}

    class Db:
        async def execute(self, *_args, **_kwargs):
            return Result()

    with pytest.raises(HTTPException) as error:
        await login(LoginRequest(email="u@example.com", password="anything"), Db())
    assert error.value.status_code == 401
