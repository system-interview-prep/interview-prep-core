from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from src.modules.job_categories.router import CategoryInput, CategoryPatch, _category, list_categories


def test_category_models_and_mapper() -> None:
    now = datetime.now(UTC)
    assert CategoryInput(name=" Engineering ").name == " Engineering "
    assert CategoryPatch().model_dump(exclude_unset=True) == {}
    assert _category({"id": "c1", "name": "Engineering", "description": "", "created_at": now, "updated_at": now})["createdAt"] == now.isoformat()
    with pytest.raises(ValidationError):
        CategoryInput(name="")


@pytest.mark.asyncio
async def test_category_list_omits_null_search_parameter() -> None:
    captured = {}

    class Result:
        def mappings(self): return self
        def all(self): return []

    class Database:
        async def execute(self, statement, params):
            captured.update(sql=str(statement), params=params)
            return Result()

    assert await list_categories({}, Database(), limit=50, q=None, cursor=None) == {"items": []}
    assert "WHERE lower(name)" not in captured["sql"]
    assert captured["params"] == {"limit": 50}


def test_category_routes_require_authentication(client) -> None:
    assert client.get("/admin/job-categories").status_code == 401
    assert client.post("/admin/job-categories", json={"name": "Engineering"}).status_code == 401
