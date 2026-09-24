from datetime import UTC, datetime
from io import BytesIO
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from starlette.datastructures import Headers, UploadFile

from src.modules.job_descriptions.router import (
    JobDescriptionPatch,
    list_job_description_versions,
    publish_job_description_version,
    update_job_description,
    get_job_description,
    get_job_description_version,
    upload_jd,
    create_job_description_version,
)
from src.workers.tasks.job_description import _parse_job_description
from src.modules.matching.router import _resolve_job


def _result(*, row=None, rows=None, rowcount=1):
    result = MagicMock()
    result.rowcount = rowcount
    result.mappings.return_value.one_or_none.return_value = row
    result.mappings.return_value.all.return_value = rows or []
    return result


@pytest.mark.asyncio
async def test_version_history_is_newest_first_and_admin_scoped() -> None:
    now = datetime.now(UTC)
    db = AsyncMock()
    db.execute.return_value = _result(
        rows=[
            {"id": "00000000-0000-0000-0000-000000000002", "job_description_id": "job-1",
             "version_number": 2, "status": "DRAFT", "processing_status": "DONE",
             "source_filename": "v2.pdf", "source_checksum": "b", "error": None,
             "created_at": now, "published_at": None},
            {"id": "00000000-0000-0000-0000-000000000001", "job_description_id": "job-1",
             "version_number": 1, "status": "ACTIVE", "processing_status": "DONE",
             "source_filename": "v1.pdf", "source_checksum": "a", "error": None,
             "created_at": now, "published_at": now},
        ]
    )
    with patch("src.modules.job_descriptions.router._get", new=AsyncMock(return_value={"id": "job-1"})):
        response = await list_job_description_versions("job-1", {"sub": "admin"}, db)

    assert [item["versionNumber"] for item in response["items"]] == [2, 1]
    assert response["items"][0]["status"] == "DRAFT"
    assert "ORDER BY version_number DESC" in str(db.execute.await_args.args[0])


@pytest.mark.asyncio
async def test_candidate_cannot_open_draft_job_directly() -> None:
    with patch(
        "src.modules.job_descriptions.router._get",
        new=AsyncMock(return_value={"id": "job-1", "listingStatus": "DRAFT"}),
    ):
        with pytest.raises(HTTPException) as caught:
            await get_job_description("job-1", {"roles": ["CANDIDATE"]}, AsyncMock())
    assert caught.value.status_code == 404


@pytest.mark.asyncio
async def test_matching_job_carries_the_active_version_snapshot_id() -> None:
    db = AsyncMock()
    db.execute.return_value = _result(row={
        "id": "job-1", "title": "Backend Engineer", "keywords": ["Python"],
        "description": "Build APIs", "requirements": "Python", "structured_data": None,
        "status": "ACTIVE", "active_version_id": "00000000-0000-0000-0000-000000000002",
    })

    job = await _resolve_job(db, "job-1")

    assert job.job_version_id == "00000000-0000-0000-0000-000000000002"
    assert "listing_status = 'ACTIVE'" in str(db.execute.await_args.args[0])


@pytest.mark.asyncio
async def test_version_detail_returns_parser_output_for_admin_preview() -> None:
    now = datetime.now(UTC)
    db = AsyncMock()
    db.execute.return_value = _result(row={
        "id": "00000000-0000-0000-0000-000000000002", "job_description_id": "job-1",
        "version_number": 2, "status": "DRAFT", "processing_status": "DONE",
        "source_filename": "v2.pdf", "source_checksum": "checksum", "error": None,
        "raw_text": "Backend JD", "structured_data": {"jobTitle": "Backend Engineer"},
        "extracted_metadata": {"extractor": "test"}, "metadata": {"title": "Backend"},
        "created_at": now, "published_at": None,
    })

    detail = await get_job_description_version(
        "job-1", "00000000-0000-0000-0000-000000000002", {"roles": ["ADMIN"]}, db
    )

    assert detail["structuredData"]["jobTitle"] == "Backend Engineer"
    assert detail["rawText"] == "Backend JD"


def _pdf_upload() -> UploadFile:
    return UploadFile(
        filename="duplicate.pdf",
        file=BytesIO(b"%PDF-1.4 duplicate content"),
        headers=Headers({"content-type": "application/pdf"}),
    )


@pytest.mark.asyncio
async def test_initial_upload_rejects_duplicate_before_storage_or_parser() -> None:
    conflict = HTTPException(status_code=409, detail="checksum duplicate")
    with (
        patch("src.modules.job_descriptions.router._file_validator.validate", return_value=("duplicate.pdf", "application/pdf")),
        patch("src.modules.job_descriptions.router._ensure_checksum_available", new=AsyncMock(side_effect=conflict)),
        patch("src.modules.job_descriptions.router.put_object") as put,
    ):
        with pytest.raises(HTTPException) as caught:
            await upload_jd({"sub": "admin-1"}, AsyncMock(), _pdf_upload())

    assert caught.value.status_code == 409
    put.assert_not_called()


@pytest.mark.asyncio
async def test_new_version_rejects_duplicate_before_storage_or_parser() -> None:
    conflict = HTTPException(status_code=409, detail="checksum duplicate")
    with (
        patch("src.modules.job_descriptions.router._get", new=AsyncMock(return_value={"id": "job-1"})),
        patch("src.modules.job_descriptions.router._file_validator.validate", return_value=("duplicate.pdf", "application/pdf")),
        patch("src.modules.job_descriptions.router._ensure_checksum_available", new=AsyncMock(side_effect=conflict)),
        patch("src.modules.job_descriptions.router.put_object") as put,
    ):
        with pytest.raises(HTTPException) as caught:
            await create_job_description_version("job-1", _pdf_upload(), {"sub": "admin-1"}, AsyncMock())

    assert caught.value.status_code == 409
    put.assert_not_called()


@pytest.mark.asyncio
async def test_publish_rejects_version_until_parser_is_done() -> None:
    db = AsyncMock()
    db.execute.side_effect = [
        _result(),
        _result(row={"id": "v2", "processing_status": "PENDING", "structured_data": None}),
    ]
    with pytest.raises(HTTPException) as caught:
        await publish_job_description_version("job-1", "00000000-0000-0000-0000-000000000002", {}, db)

    assert caught.value.status_code == 409
    db.rollback.assert_awaited_once()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_publish_is_transactional_and_switches_active_snapshot() -> None:
    db = AsyncMock()
    db.execute.side_effect = [
        _result(),
        _result(row={"id": "v2", "processing_status": "DONE", "structured_data": {"schemaVersion": "1.0"}}),
        _result(),
        _result(),
        _result(),
    ]
    with patch("src.modules.job_descriptions.router._get", new=AsyncMock(return_value={"id": "job-1"})):
        await publish_job_description_version(
            "job-1", "00000000-0000-0000-0000-000000000002", {}, db
        )

    sql = "\n".join(str(call.args[0]) for call in db.execute.await_args_list)
    assert "status = 'SUPERSEDED'" in sql
    assert "status = 'ACTIVE'" in sql
    assert "active_version_id" in sql
    assert "structured_data = v.structured_data" in sql
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_metadata_only_edit_does_not_create_a_version() -> None:
    db = AsyncMock()
    db.execute.side_effect = [_result(), _result()]
    with patch("src.modules.job_descriptions.router._get", new=AsyncMock(return_value={"id": "job-1"})):
        await update_job_description(
            "job-1", JobDescriptionPatch(title="Updated title"), {}, db
        )

    sql = "\n".join(str(call.args[0]) for call in db.execute.await_args_list)
    assert "INSERT INTO job_description_versions" not in sql
    assert "UPDATE job_description_versions SET metadata" in sql
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_worker_copies_parsed_upload_into_requested_version_only() -> None:
    db = AsyncMock()
    session_factory = MagicMock()
    session_factory.return_value.__aenter__ = AsyncMock(return_value=db)
    session_factory.return_value.__aexit__ = AsyncMock(return_value=False)
    pipeline = MagicMock()
    pipeline.run = AsyncMock(return_value=MagicMock(status="DONE", upload_id="upload-2"))

    with (
        patch("src.workers.tasks.job_description.SessionFactory", session_factory),
        patch("src.workers.tasks.job_description.load_active_skill_taxonomy", new=AsyncMock(
            return_value=MagicMock(skills=[], version="v1")
        )),
        patch("src.workers.tasks.job_description.get_settings", return_value=MagicMock(jd_parser_mode="deterministic")),
        patch("src.workers.tasks.job_description.JobDescriptionParsingPipeline", return_value=pipeline),
        patch("src.workers.tasks.job_description.DeterministicJobDescriptionParser"),
        patch("src.workers.tasks.job_description.SqlAlchemyJobDescriptionParseRepository"),
        patch("src.workers.tasks.job_description.R2ObjectStorage"),
        patch("src.workers.tasks.job_description.MinerUDocumentExtractor"),
    ):
        response = await _parse_job_description(
            "upload-2", "00000000-0000-0000-0000-000000000002"
        )

    calls = db.execute.await_args_list
    assert "processing_status = 'PROCESSING'" in str(calls[0].args[0])
    copy_call = calls[-1]
    assert "UPDATE job_description_versions" in str(copy_call.args[0])
    assert "v.source_upload_id = u.id" in str(copy_call.args[0])
    assert copy_call.args[1]["upload_id"] == "upload-2"
    assert response["version_id"] == "00000000-0000-0000-0000-000000000002"
    assert db.commit.await_count == 2
