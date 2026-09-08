import json

from src.modules.user_cvs.parsing.application.pipeline import CvDocument
from src.modules.user_cvs.parsing.infrastructure.repository import SqlAlchemyCvParseRepository
from src.modules.user_cvs.domain.schemas import CanonicalResume, ParsedResume


class RecordingSession:
    def __init__(self) -> None:
        self.calls = []
        self.commits = 0

    async def execute(self, statement, parameters):
        self.calls.append((str(statement), parameters))

    async def commit(self) -> None:
        self.commits += 1


class MappingResult:
    def __init__(self, row) -> None:
        self._row = row

    def mappings(self):
        return self

    def one_or_none(self):
        return self._row


class ClaimSession(RecordingSession):
    async def execute(self, statement, parameters):
        self.calls.append((str(statement), parameters))
        return MappingResult(
            {
                "id": "cv-1",
                "filename": "resume.pdf",
                "storage_key": "cvs/cv-1.pdf",
                "checksum": "d" * 64,
            }
        )


async def test_claim_is_atomic_and_only_accepts_retryable_states() -> None:
    session = ClaimSession()
    repository = SqlAlchemyCvParseRepository(session)  # type: ignore[arg-type]

    document = await repository.claim("cv-1")

    statement, parameters = session.calls[0]
    assert "status IN ('PENDING', 'FAILED')" in statement
    assert "RETURNING id, filename, storage_key, checksum" in statement
    assert parameters == {"id": "cv-1"}
    assert document is not None and document.storage_key == "cvs/cv-1.pdf"
    assert session.commits == 1


async def test_complete_serializes_canonical_resume_into_parsed_data_jsonb() -> None:
    session = RecordingSession()
    repository = SqlAlchemyCvParseRepository(session)  # type: ignore[arg-type]
    document = CvDocument("cv-1", "resume.pdf", "cvs/cv-1.pdf", "d" * 64)
    parsed = ParsedResume(
        resume=CanonicalResume(
            schemaVersion="2.1",
            resumeId="cv-1",
            documentId="cv-1",
            documentSha256="d" * 64,
        )
    )

    await repository.complete(
        document,
        raw_text="Skills\nPython",
        parsed=parsed,
        parse_source="mineru+deterministic-v4",
    )

    statement, parameters = session.calls[0]
    assert "parsed_data = CAST(:parsed_data AS jsonb)" in statement
    assert parameters["id"] == "cv-1"
    assert json.loads(parameters["parsed_data"])["schemaVersion"] == "2.1"
    assert session.commits == 1
