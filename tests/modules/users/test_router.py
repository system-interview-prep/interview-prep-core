from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from src.modules.users.router import ProfilePatch, _profile
from src.modules.users.service import UserNotFoundError, UserService


def profile_row() -> dict:
    return {
        "id": "u1",
        "email": "u@example.com",
        "name": "User",
        "role": "CANDIDATE",
        "provider": "local",
        "dob": None,
        "avatar_url": "https://images.test/user.png",
        "created_at": datetime.now(UTC),
        "cv_scans_remaining": 3,
        "voice_mock_remaining": 1,
    }


def test_profile_mapper_exposes_avatar_and_onboarding_credits() -> None:
    row = profile_row()
    profile = _profile(row)

    assert profile["createdAt"] == row["created_at"].isoformat()
    assert profile["created_at"] == row["created_at"].isoformat()
    assert profile["picture"] == profile["avatar"] == "https://images.test/user.png"
    assert profile["credits"] == {
        "cvScansRemaining": 3,
        "mockSessionsRemaining": 1,
    }
    assert ProfilePatch(name="  Candidate  ").name == "Candidate"
    with pytest.raises(ValidationError):
        ProfilePatch(name="x" * 256)
    with pytest.raises(ValidationError):
        ProfilePatch(name="   ")


def test_user_profile_aliases_require_authentication_and_use_standard_error(client) -> None:
    for path in ("/users/me", "/user/profile"):
        response = client.get(path)
        assert response.status_code == 401
        assert response.json() == {"message": "Chưa xác thực.", "statusCode": 401}

        response = client.patch(path, json={"name": "User"})
        assert response.status_code == 401


@pytest.mark.asyncio
async def test_user_service_reads_and_updates_profile() -> None:
    class Repository:
        def __init__(self):
            self.row = profile_row()
            self.updated = None
            self.commits = 0

        async def get_profile(self, user_id):
            return self.row if user_id == "u1" else None

        async def update_profile(self, user_id, **values):
            self.updated = (user_id, values)
            self.row["name"] = values["name"] or self.row["name"]

        async def commit(self):
            self.commits += 1

    repository = Repository()
    service = UserService(repository)

    assert (await service.get_profile("u1"))["credits"]["cvScansRemaining"] == 3
    updated = await service.update_profile("u1", ProfilePatch(name="New Name"))
    assert repository.updated == ("u1", {"name": "New Name", "dob": None})
    assert repository.commits == 1
    assert updated["name"] == "New Name"

    with pytest.raises(UserNotFoundError):
        await service.get_profile("missing")


@pytest.mark.asyncio
async def test_user_service_updates_avatar() -> None:
    class Repository:
        def __init__(self):
            self.row = profile_row()
            self.avatar_url = None
            self.commits = 0

        async def get_profile(self, user_id):
            return self.row if user_id == "u1" else None

        async def update_avatar(self, user_id, avatar_url):
            self.avatar_url = avatar_url
            self.row["avatar_url"] = avatar_url

        async def commit(self):
            self.commits += 1

    repository = Repository()
    service = UserService(repository)

    updated = await service.update_avatar("u1", "data:image/png;base64,AAAA")
    assert repository.avatar_url == "data:image/png;base64,AAAA"
    assert repository.commits == 1
    assert updated["picture"] == updated["avatar"] == "data:image/png;base64,AAAA"
