from types import SimpleNamespace

import pytest

from src.modules.user_cvs.parsing.domain.artifacts import DocumentArtifacts
from src.workers.tasks import job_description


class _Result:
    def __init__(self, row: dict | None = None) -> None:
        self._row = row

    def mappings(self) -> "_Result":
        return self

    def one_or_none(self) -> dict | None:
        return self._row


class _Database:
    def __init__(self, row: dict) -> None:
        self.row = row
        self.statements: list[str] = []
        self.commits = 0

    async def __aenter__(self) -> "_Database":
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def execute(self, statement, _params=None):
        sql = str(statement)
        self.statements.append(sql)
        if "SELECT filename, storage_key, checksum" in sql:
            return _Result(self.row)
        return SimpleNamespace(rowcount=1)

    async def commit(self) -> None:
        self.commits += 1


def _row() -> dict:
    return {"filename": "backend-jd.docx", "storage_key": "jd/up-1.docx", "checksum": "a" * 64}


def _artifacts() -> DocumentArtifacts:
    return DocumentArtifacts(
        markdown="Job Title: Backend Engineer\nRequirements\n- Java\n",
        content_list=[
            {"type": "text", "text": "Job Title: Backend Engineer", "page_idx": 0},
            {"type": "text", "text": "Requirements", "page_idx": 0},
            {"type": "text", "text": "- Java", "page_idx": 0},
        ],
        extractor_version="mineru-test",
    )


@pytest.mark.asyncio
async def test_parse_job_description_persists_artifact_and_canonical_payload(monkeypatch) -> None:
    db = _Database(_row())
    written: list[tuple[str, bytes, str]] = []

    async def extract(*_: object) -> DocumentArtifacts:
        return _artifacts()

    monkeypatch.setattr(job_description, "SessionFactory", lambda: db)
    monkeypatch.setattr(job_description, "get_object", lambda _: b"document")
    monkeypatch.setattr(job_description, "extract_document_artifacts", extract)
    monkeypatch.setattr(
        job_description,
        "put_object",
        lambda key, body, content_type: written.append((key, body, content_type)),
    )

    result = await job_description._parse_job_description("up-1")

    assert result == {"status": "DONE", "upload_id": "up-1"}
    assert len(written) == 1
    assert written[0][0].endswith(".artifacts/" + "a" * 64 + "/mineru.json")
    assert any("status = 'PARSING'" in sql for sql in db.statements)
    assert any("status = 'DONE'" in sql and "structured_data" in sql for sql in db.statements)
    assert db.commits == 2


@pytest.mark.asyncio
async def test_parse_job_description_records_failure_for_review(monkeypatch) -> None:
    db = _Database(_row())

    async def extraction_failure(*_: object) -> DocumentArtifacts:
        raise RuntimeError("MinerU unavailable")

    monkeypatch.setattr(job_description, "SessionFactory", lambda: db)
    monkeypatch.setattr(job_description, "get_object", lambda _: b"document")
    monkeypatch.setattr(job_description, "extract_document_artifacts", extraction_failure)

    with pytest.raises(RuntimeError, match="MinerU unavailable"):
        await job_description._parse_job_description("up-1")

    assert any("status = 'PARSING'" in sql for sql in db.statements)
    assert any("status = 'FAILED'" in sql for sql in db.statements)
    assert db.commits == 2


@pytest.mark.asyncio
async def test_parse_job_description_ignores_empty_upload_id() -> None:
    assert await job_description._parse_job_description("") == {"status": "ignored"}
