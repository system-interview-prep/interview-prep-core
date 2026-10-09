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


def test_import_maps_skill_and_role_columns_for_the_selector() -> None:
    """P2 matches skill targets on TARGET_SKILL and career fallback on TARGET_ROLE."""
    mappings = QuestionImportService._taxonomy_mappings({
        "primary_competency_id": "backend-developer",
        "skill_ids": ["skill-python", "skill-python"],
        "target_role_ids": ["backend-developer"],
        "supporting_competency_ids": [],
    })
    assert [(m.concept_id, m.purpose) for m in mappings] == [
        ("backend-developer", "PRIMARY_COMPETENCY"),
        ("skill-python", "TARGET_SKILL"),
        ("backend-developer", "TARGET_ROLE"),
    ]


@pytest.mark.asyncio
async def test_import_mappings_use_active_taxonomy_kind_validation() -> None:
    from src.modules.question_bank.service import QuestionBankService

    payload = {
        "primary_competency_id": "comp",
        "skill_ids": ["skill-python"],
        "target_role_ids": ["comp"],
    }
    mappings = QuestionImportService._taxonomy_mappings(payload)
    valid = TaxonomyDb([
        {"concept_id": "comp", "kind": "competency"},
        {"concept_id": "skill-python", "kind": "skill"},
    ])
    await QuestionBankService(valid)._validate_taxonomy_mappings("v1", mappings)

    invalid = TaxonomyDb([
        {"concept_id": "comp", "kind": "skill"},
        {"concept_id": "skill-python", "kind": "skill"},
    ])
    with pytest.raises(HTTPException, match="invalid or inactive concept kind"):
        await QuestionBankService(invalid)._validate_taxonomy_mappings("v1", mappings)
