from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from src.modules.users.router import ProfilePatch, _profile


def test_profile_mapper_and_validation() -> None:
    now = datetime.now(UTC)
    profile = _profile(
        {
            "id": "u1",
            "email": "u@example.com",
            "name": "U",
            "role": "CANDIDATE",
            "provider": "local",
            "dob": None,
            "picture": None,
            "created_at": now,
        }
    )
    assert profile["createdAt"] == now.isoformat()
    assert ProfilePatch(name="A").name == "A"
    with pytest.raises(ValidationError):
        ProfilePatch(name="x" * 256)


def test_user_routes_require_authentication(client) -> None:
    assert client.get("/users/me").status_code == 401
    assert client.patch("/users/me", json={"name": "User"}).status_code == 401
