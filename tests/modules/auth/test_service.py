import pytest

from src.modules.auth.repository import AuthRepository
from src.modules.auth.schemas import RegisterRequest
from src.modules.auth.service import (
    AuthService,
    EmailAlreadyExistsError,
    InvalidCredentialsError,
)


class FakeRepository:
    def __init__(self, user=None):
        self.user = user
        self.created_local = None
        self.created_google = None
        self.linked_google = None
        self.commits = 0
        self.rollbacks = 0

    async def get_by_email(self, _email):
        return self.user

    async def create_local(self, **values):
        self.created_local = values
        return {
            "id": "user-1",
            "email": values["email"],
            "name": values["name"],
            "role": "CANDIDATE",
            "provider": "local",
            "avatar_url": None,
        }

    async def create_google(self, **values):
        self.created_google = values
        return {
            "id": "user-google",
            "email": values["email"],
            "name": values["name"],
            "role": "CANDIDATE",
            "provider": "google",
            "avatar_url": values["avatar_url"],
            "is_active": True,
        }

    async def link_google_identity(self, user_id, **values):
        self.linked_google = (user_id, values)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


@pytest.mark.asyncio
async def test_register_hashes_password_and_returns_token(monkeypatch) -> None:
    repository = FakeRepository()
    monkeypatch.setattr("src.modules.auth.service.hash_password", lambda _password: "bcrypt-hash")
    monkeypatch.setattr("src.modules.auth.service.create_access_token", lambda *_args: "jwt-token")
    service = AuthService(repository, "https://google.test/userinfo")

    response = await service.register(
        RegisterRequest(name="Candidate", email="USER@example.com", password="Strong123")
    )

    assert repository.created_local == {
        "name": "Candidate",
        "email": "user@example.com",
        "password_hash": "bcrypt-hash",
        "phone": None,
    }
    assert repository.commits == 1
    assert response["access_token"] == "jwt-token"
    assert response["user"]["role"] == "CANDIDATE"


@pytest.mark.asyncio
async def test_register_rejects_duplicate_email() -> None:
    service = AuthService(FakeRepository(user={"id": "existing"}), "https://google.test/userinfo")
    with pytest.raises(EmailAlreadyExistsError):
        await service.register(
            RegisterRequest(name="Candidate", email="user@example.com", password="Strong123")
        )


@pytest.mark.asyncio
async def test_login_rejects_wrong_password_and_accepts_local_or_linked_google(monkeypatch) -> None:
    user = {
        "id": "user-1",
        "email": "user@example.com",
        "password_hash": "hash",
        "name": "Candidate",
        "role": "CANDIDATE",
        "provider": "local",
        "avatar_url": None,
        "is_active": True,
    }
    service = AuthService(FakeRepository(user=user), "https://google.test/userinfo")
    monkeypatch.setattr("src.modules.auth.service.verify_password", lambda password, _hash: password == "ok")
    monkeypatch.setattr("src.modules.auth.service.create_access_token", lambda *_args: "jwt-token")

    with pytest.raises(InvalidCredentialsError):
        await service.login("user@example.com", "wrong")
    assert (await service.login("user@example.com", "ok"))["access_token"] == "jwt-token"


@pytest.mark.asyncio
async def test_google_login_creates_candidate_and_links_existing_account(monkeypatch) -> None:
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "sub": "google-123",
                "email": "USER@example.com",
                "email_verified": True,
                "name": "Google User",
                "picture": "https://images.test/google.png",
            }

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, _url, **_kwargs):
            return Response()

    monkeypatch.setattr("src.modules.auth.service.httpx.AsyncClient", lambda **_kwargs: Client())
    monkeypatch.setattr("src.modules.auth.service.create_access_token", lambda *_args: "jwt-token")

    new_repository = FakeRepository()
    response = await AuthService(new_repository, "https://google.test/userinfo").google_login("token")
    assert new_repository.created_google["email"] == "user@example.com"
    assert new_repository.created_google["google_id"] == "google-123"
    assert response["user"]["picture"] == "https://images.test/google.png"

    existing = {
        "id": "local-1",
        "email": "user@example.com",
        "password_hash": "hash",
        "name": "Existing",
        "role": "CANDIDATE",
        "provider": "local",
        "avatar_url": None,
        "google_id": None,
        "is_active": True,
    }
    existing_repository = FakeRepository(user=existing)
    await AuthService(existing_repository, "https://google.test/userinfo").google_login("token")
    assert existing_repository.linked_google == (
        "local-1",
        {"google_id": "google-123", "avatar_url": "https://images.test/google.png"},
    )


@pytest.mark.asyncio
async def test_repository_creates_user_and_initial_wallet_in_same_transaction() -> None:
    class Session:
        def __init__(self):
            self.calls = []
            self.commits = 0

        async def execute(self, statement, parameters):
            self.calls.append((str(statement), parameters))

        async def commit(self):
            self.commits += 1

    session = Session()
    repository = AuthRepository(session)
    await repository.create_local(
        name="Candidate",
        email="user@example.com",
        password_hash="hash",
        phone=None,
    )
    await repository.commit()

    assert "INSERT INTO users" in session.calls[0][0]
    assert "INSERT INTO user_credits" in session.calls[1][0]
    assert session.calls[1][1]["user_id"] == session.calls[0][1]["id"]
    assert session.commits == 1
