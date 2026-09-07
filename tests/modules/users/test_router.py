from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from src.modules.users.router import ProfilePatch, _profile


def test_profile_mapper_and_validation() -> None:
    now = datetime.now(UTC)
    profile = _profile({"id": "u1", "email": "u@example.com", "name": "U", "role": "CANDIDATE", "provider": "local", "dob": None, "picture": None, "created_at": now})
    assert profile["created_at"] == now.isoformat()
    assert ProfilePatch(name="A").name == "A"
    with pytest.raises(ValidationError):
        ProfilePatch(name="x" * 256)


def test_user_routes_require_authentication(client) -> None:
    assert client.get("/user/profile").status_code == 401
    assert client.patch("/user/profile", json={"name": "User"}).status_code == 401
