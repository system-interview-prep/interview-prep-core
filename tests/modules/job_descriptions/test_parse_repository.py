import json

from src.modules.job_descriptions.domain.schemas import CanonicalJobDescription
from src.modules.job_descriptions.parsing.application.pipeline import JobDescriptionDocument
from src.modules.job_descriptions.parsing.infrastructure.repository import (
    SqlAlchemyJobDescriptionParseRepository,
)
from src.modules.user_cvs.domain.schemas import ParsingMetadata


class Result:
    def __init__(self, row=None, rowcount=1):
        self.row = row
        self.rowcount = rowcount

    def mappings(self):
        return self

    def one_or_none(self):
        return self.row


class Session:
    def __init__(self, row=None):
        self.row = row
        self.calls = []
        self.commits = 0
        self.rollbacks = 0

    async def execute(self, statement, parameters):
        self.calls.append((str(statement), parameters))
        return Result(self.row)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


async def test_jd_repository_claim_is_atomic_and_state_guarded() -> None:
    session = Session(
        {
            "id": "jd-1",
            "filename": "jd.pdf",
            "storage_key": "jd/jd.pdf",
            "checksum": "a" * 64,
        }
    )
    document = await SqlAlchemyJobDescriptionParseRepository(session).claim("jd-1")
    statement, _ = session.calls[0]
    assert "status IN ('PENDING', 'FAILED')" in statement
    assert "RETURNING id, filename, storage_key, checksum" in statement
    assert document.upload_id == "jd-1"
    assert session.commits == 1


async def test_jd_repository_complete_serializes_canonical_payload() -> None:
    session = Session()
    repository = SqlAlchemyJobDescriptionParseRepository(session)
    parsed = CanonicalJobDescription(
        schemaVersion="1.0",
        parsing=ParsingMetadata(
            parserVersion="test",
            extractionVersion="test",
            parsedAt="2026-01-01T00:00:00Z",
            status="review_required",
        ),
    )
    await repository.complete(
        JobDescriptionDocument("jd-1", "jd.pdf", "jd/jd.pdf", "a" * 64),
        raw_text="raw",
        parsed=parsed,
        parse_source="mineru+test",
    )
    statement, values = session.calls[0]
    assert "status = 'DONE'" in statement
    assert json.loads(values["structured_data"])["schemaVersion"] == "1.0"
    assert session.commits == 1


async def test_jd_repository_rolls_back_when_upload_cannot_be_claimed() -> None:
    session = Session(None)
    assert await SqlAlchemyJobDescriptionParseRepository(session).claim("missing") is None
    assert session.rollbacks == 1
