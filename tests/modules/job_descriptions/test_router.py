from datetime import UTC, datetime

import pytest

from src.modules.job_descriptions.router import (
    JobDescriptionPatch,
    _decode_cursor,
    _encode_cursor,
    _job_description,
    _upload,
)


def test_job_description_cursor_and_mappers() -> None:
    now = datetime.now(UTC)
    cursor = _encode_cursor({"id": "jp-1", "createdAt": now.isoformat()})
    assert _decode_cursor(cursor) == (now, "jp-1")
    with pytest.raises(Exception) as error:
        _decode_cursor("invalid")
    assert getattr(error.value, "status_code", None) == 422
    job_description = _job_description(
        {
            "id": "jp-1",
            "primary_taxonomy_version": "internal-2026.2",
            "primary_taxonomy_concept_id": "occupation.backend-engineer",
            "title": "Backend",
            "keywords": ["Python"],
            "description": "desc",
            "structured_data": {"seniority": "mid"},
            "extracted_metadata": None,
            "raw_text": "raw",
            "status": "ACTIVE",
            "created_at": now,
            "updated_at": now,
            "taxonomy_label": "Backend Engineering",
            "taxonomy_kind": "occupation",
        }
    )
    assert job_description["primaryTaxonomy"]["conceptId"] == "occupation.backend-engineer"
    assert job_description["structuredData"] == {"seniority": "mid"}
    assert JobDescriptionPatch().model_dump(exclude_unset=True) == {}
    assert (
        _upload(
            {
                "id": "up-1",
                "owner_user_id": "u1",
                "filename": "jd.pdf",
                "content_type": "application/pdf",
                "size": 3,
                "status": "DONE",
                "parse_source": "mineru",
                "raw_text": "raw",
                "description": "desc",
                "error": None,
                "created_at": now,
                "updated_at": now,
            }
        )["parseSource"]
        == "mineru"
    )


@pytest.mark.parametrize(
    ("method", "path", "kwargs"),
    [
        ("get", "/admin/job-descriptions", {}),
        (
            "post",
            "/admin/job-descriptions/uploads",
            {"files": {"file": ("jd.pdf", b"pdf", "application/pdf")}},
        ),
        ("get", "/admin/job-descriptions/uploads/up-1", {}),
        ("get", "/admin/job-descriptions/uploads/up-1/events", {}),
        ("get", "/admin/job-descriptions/jd-1", {}),
    ],
)
def test_job_description_routes_require_authentication(
    client, method: str, path: str, kwargs: dict
) -> None:
    assert getattr(client, method)(path, **kwargs).status_code == 401
