import pytest
from fastapi import HTTPException

from src.modules.taxonomy.admin_service import TaxonomyAdminService
from src.modules.taxonomy.schemas import TaxonomyVersionUpsert


class EmptyTaxonomyDb:
    def __init__(self, active_skill_count: int) -> None:
        self.active_skill_count = active_skill_count
        self.executed = False

    async def scalar(self, *_args, **_kwargs) -> int:
        return self.active_skill_count

    async def execute(self, *_args, **_kwargs):
        self.executed = True


@pytest.mark.asyncio
async def test_upsert_activation_rejects_version_without_active_skill() -> None:
    db = EmptyTaxonomyDb(0)

    with pytest.raises(HTTPException, match="needs an active skill"):
        await TaxonomyAdminService(db).upsert_version(
            TaxonomyVersionUpsert(version="v1", activate=True)
        )

    assert db.executed is False
