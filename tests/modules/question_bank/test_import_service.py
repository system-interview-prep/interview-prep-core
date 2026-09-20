import pytest
from fastapi import HTTPException

from src.modules.question_bank.import_service import (
    MAX_IMPORT_FILE_SIZE,
    QuestionImportService,
)


class BoundedUpload:
    filename = "questions.csv"

    def __init__(self, content: bytes) -> None:
        self.content = content
        self.read_size: int | None = None

    async def read(self, size: int = -1) -> bytes:
        self.read_size = size
        return self.content[:size]


class TaxonomyResult:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows

    def mappings(self) -> "TaxonomyResult":
        return self

    def __iter__(self):
        return iter(self.rows)


class TaxonomyDb:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows

    async def execute(self, *_args, **_kwargs) -> TaxonomyResult:
        return TaxonomyResult(self.rows)


@pytest.mark.asyncio
async def test_create_import_reads_only_size_limit_plus_one_byte() -> None:
    upload = BoundedUpload(b"x" * (MAX_IMPORT_FILE_SIZE + 2))
    service = QuestionImportService(db=None)

    with pytest.raises(HTTPException, match="between 1 byte and 5 MB"):
        await service.create_csv_import(upload, "actor-1")

    assert upload.read_size == MAX_IMPORT_FILE_SIZE + 1


@pytest.mark.asyncio
async def test_import_primary_competency_uses_active_competency_validation() -> None:
    service = QuestionImportService(TaxonomyDb([{"concept_id": "comp", "kind": "competency"}]))
    await service._validate_primary_competency({"taxonomy_version": "v1", "primary_competency_id": "comp"})

    invalid = QuestionImportService(TaxonomyDb([{"concept_id": "comp", "kind": "skill"}]))
    with pytest.raises(HTTPException, match="invalid or inactive concept kind"):
        await invalid._validate_primary_competency(
            {"taxonomy_version": "v1", "primary_competency_id": "comp"}
        )
