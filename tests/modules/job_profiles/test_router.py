from datetime import UTC, datetime

import pytest

from src.modules.job_profiles.router import JobProfilePatch, _decode_cursor, _encode_cursor, _json_value, _profile, _upload


def test_profile_cursor_and_mappers() -> None:
    now = datetime.now(UTC)
    cursor = _encode_cursor({"id": "jp-1", "createdAt": now.isoformat()})
    assert _decode_cursor(cursor) == (now, "jp-1")
    with pytest.raises(Exception) as error:
        _decode_cursor("invalid")
    assert getattr(error.value, "status_code", None) == 422
    profile = _profile({"id": "jp-1", "category_id": "c1", "title": "Backend", "keywords": ["Python"], "description": "desc", "ai_profile_ui_json": {"seniority": "mid"}, "ai_extras_json": None, "raw_jd_text": "raw", "status": "ACTIVE", "created_at": now, "updated_at": now, "category_name": "Engineering", "category_description": "Tech"})
    assert profile["category"]["name"] == "Engineering"
    assert profile["aiProfileUiJson"] == '{"seniority": "mid"}'
    assert _json_value(None) is None
    assert JobProfilePatch().model_dump(exclude_unset=True) == {}
    assert _upload({"id": "up-1", "owner_user_id": "u1", "filename": "jd.pdf", "content_type": "application/pdf", "size": 3, "status": "DONE", "parse_source": "mineru", "raw_jd_text": "raw", "description": "desc", "error": None, "created_at": now, "updated_at": now})["parseSource"] == "mineru"


@pytest.mark.parametrize(
    ("method", "path", "kwargs"),
    [
        ("get", "/admin/job-profiles", {}),
        ("post", "/admin/job-profiles/uploads", {"files": {"file": ("jd.pdf", b"pdf", "application/pdf")}}),
        ("get", "/admin/job-profiles/uploads/up-1", {}),
        ("get", "/admin/job-profiles/jp-1", {}),
    ],
)
def test_job_profile_routes_require_authentication(client, method: str, path: str, kwargs: dict) -> None:
    assert getattr(client, method)(path, **kwargs).status_code == 401
