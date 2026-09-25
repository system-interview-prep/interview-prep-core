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


class DeleteTaxonomyDb:
    def __init__(self, is_active: bool | None) -> None:
        self.is_active = is_active
        self.deleted = False
        self.committed = False

    async def scalar(self, *_args, **_kwargs) -> bool | None:
        return self.is_active

    async def execute(self, *_args, **_kwargs) -> None:
        self.deleted = True

    async def commit(self) -> None:
        self.committed = True


@pytest.mark.asyncio
async def test_upsert_activation_rejects_version_without_active_skill() -> None:
    db = EmptyTaxonomyDb(0)

    with pytest.raises(HTTPException, match="needs an active skill"):
        await TaxonomyAdminService(db).upsert_version(
            TaxonomyVersionUpsert(version="v1", activate=True)
        )

    assert db.executed is False


@pytest.mark.asyncio
async def test_delete_version_rejects_the_active_version() -> None:
    db = DeleteTaxonomyDb(is_active=True)

    with pytest.raises(HTTPException, match="Cannot delete the active"):
        await TaxonomyAdminService(db).delete_version("v1")

    assert db.deleted is False
    assert db.committed is False


@pytest.mark.asyncio
async def test_delete_version_removes_an_inactive_version() -> None:
    db = DeleteTaxonomyDb(is_active=False)

    result = await TaxonomyAdminService(db).delete_version("v1")

    assert result == {"version": "v1", "deleted": True}
    assert db.deleted is True
    assert db.committed is True


@pytest.mark.asyncio
async def test_delete_concept_removes_an_existing_concept() -> None:
    db = DeleteTaxonomyDb(is_active=True)

    result = await TaxonomyAdminService(db).delete_concept("v1", "skill-python")

    assert result == {
        "version": "v1",
        "conceptId": "skill-python",
        "deleted": True,
    }
    assert db.deleted is True
    assert db.committed is True
