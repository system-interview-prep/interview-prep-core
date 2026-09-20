import pytest

import src.seeds as seeds


@pytest.mark.asyncio
async def test_run_all_seeds_runs_admin_before_taxonomy(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    async def admin(*_args, **_kwargs) -> bool:
        calls.append("admin")
        return True

    async def taxonomy(*_args, **_kwargs) -> bool:
        calls.append("taxonomy")
        return True

    monkeypatch.setattr(seeds, "seed_admin", admin)
    monkeypatch.setattr(seeds, "seed_taxonomy", taxonomy)

    assert await seeds.run_all_seeds(lambda: None) is True
    assert calls == ["admin", "taxonomy"]
