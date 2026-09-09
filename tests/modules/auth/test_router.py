import importlib

import pytest
from pydantic import ValidationError

from src.modules.auth.rate_limit import login_rate_limiter
from src.modules.auth.router import GoogleLoginRequest, LoginRequest, RegisterRequest, _public_user
from src.modules.auth.service import EmailAlreadyExistsError, InvalidCredentialsError


@pytest.fixture(autouse=True)
def reset_login_rate_limiter() -> None:
    login_rate_limiter.clear()


def test_auth_models_normalize_and_validate_credentials() -> None:
    payload = RegisterRequest(
        name="  Nguyễn Minh Anh  ",
        email=" USER@EXAMPLE.COM ",
        password="Strong123",
    )
    assert payload.name == "Nguyễn Minh Anh"
    assert payload.email == "user@example.com"
    assert payload.role == "CANDIDATE"
    assert LoginRequest(email=" USER@EXAMPLE.COM ", password="secret").email == "user@example.com"
    assert GoogleLoginRequest(token="token").token == "token"
    assert GoogleLoginRequest(accessToken="legacy-token").token == "legacy-token"

    invalid_registrations = [
        {"name": "", "email": "user@example.com", "password": "Strong123"},
        {"name": "User", "email": "invalid", "password": "Strong123"},
        {"name": "User", "email": "user@example.com", "password": "lowercase1"},
        {"name": "User", "email": "user@example.com", "password": "UPPERCASE1"},
        {"name": "User", "email": "user@example.com", "password": "NoNumberHere"},
    ]
    for invalid in invalid_registrations:
        with pytest.raises(ValidationError):
            RegisterRequest(**invalid)
    with pytest.raises(ValidationError):
        GoogleLoginRequest(token=" ")


def test_auth_public_user_excludes_sensitive_columns() -> None:
    row = {
        "id": "u1",
        "email": "u@example.com",
        "name": "User",
        "role": "CANDIDATE",
        "provider": "local",
        "password_hash": "secret",
        "avatar_url": "https://images.test/user.png",
    }
    assert _public_user(row) == {
        "id": "u1",
        "email": "u@example.com",
        "name": "User",
        "role": "CANDIDATE",
        "provider": "local",
        "picture": "https://images.test/user.png",
        "avatar": "https://images.test/user.png",
    }


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        ("/auth/register", {"name": "User", "email": "x", "password": "123"}),
        ("/auth/login", {"email": "x"}),
        ("/auth/google", {"token": ""}),
    ],
)
def test_auth_routes_reject_invalid_payloads_with_standard_error(client, path: str, payload: dict) -> None:
    response = client.post(path, json=payload)
    assert response.status_code == 422
    assert response.json()["statusCode"] == 422
    assert response.json()["message"]


def test_register_conflict_uses_frontend_error_contract(client, monkeypatch) -> None:
    auth_router = importlib.import_module("src.modules.auth.router")

    class Service:
        async def register(self, _payload):
            raise EmailAlreadyExistsError

    monkeypatch.setattr(auth_router, "_service", lambda _db: Service())
    response = client.post(
        "/auth/register",
        json={"name": "User", "email": "user@example.com", "password": "Strong123"},
    )
    assert response.status_code == 409
    assert response.json() == {"message": "Email đã được đăng ký.", "statusCode": 409}


def test_login_rate_limit_blocks_sixth_failed_attempt(client, monkeypatch) -> None:
    auth_router = importlib.import_module("src.modules.auth.router")

    class Service:
        async def login(self, _email, _password):
            raise InvalidCredentialsError

    monkeypatch.setattr(auth_router, "_service", lambda _db: Service())
    payload = {"email": "user@example.com", "password": "wrong-password"}

    for _ in range(5):
        response = client.post("/auth/login", json=payload)
        assert response.status_code == 401
        assert response.json()["message"] == "Email hoặc mật khẩu không chính xác."

    blocked = client.post("/auth/login", json=payload)
    assert blocked.status_code == 429
    assert blocked.json()["statusCode"] == 429
