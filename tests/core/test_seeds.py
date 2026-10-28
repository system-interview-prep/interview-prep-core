from types import SimpleNamespace

import pytest

import src.seeds as seeds


def _settings(app_env: str) -> SimpleNamespace:
    return SimpleNamespace(app_env=app_env)


@pytest.mark.asyncio
async def test_run_all_seeds_runs_admin_before_taxonomy(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    async def admin(*_args, **_kwargs) -> bool:
        calls.append("admin")
        return True

    async def taxonomy(*_args, **_kwargs) -> bool:
        calls.append("taxonomy")
        return True

    async def question_bank(*_args, **_kwargs) -> dict[str, int]:
        calls.append("question_bank")
        return {}

    monkeypatch.setattr(seeds, "seed_admin", admin)
    monkeypatch.setattr(seeds, "seed_taxonomy", taxonomy)
    monkeypatch.setattr(seeds, "seed_question_bank", question_bank)

    assert await seeds.run_all_seeds(lambda: None, _settings("development")) is True
    assert calls == ["admin", "taxonomy", "question_bank"]


@pytest.mark.asyncio
@pytest.mark.parametrize("app_env", ["production", "PRODUCTION", "staging", ""])
async def test_question_bank_fixtures_are_not_seeded_outside_dev(
    monkeypatch: pytest.MonkeyPatch, app_env: str
) -> None:
    """Dev/demo questions must never land in a non-development database."""
    calls: list[str] = []

    async def admin(*_args, **_kwargs) -> bool:
        calls.append("admin")
        return True

    async def taxonomy(*_args, **_kwargs) -> bool:
        calls.append("taxonomy")
        return True

    async def question_bank(*_args, **_kwargs) -> dict[str, int]:
        calls.append("question_bank")
        return {}

    monkeypatch.setattr(seeds, "seed_admin", admin)
    monkeypatch.setattr(seeds, "seed_taxonomy", taxonomy)
    monkeypatch.setattr(seeds, "seed_question_bank", question_bank)

    await seeds.run_all_seeds(lambda: None, _settings(app_env))

    assert calls == ["admin", "taxonomy"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("app_env", "enabled", "expected"),
    [("production", True, True), ("development", False, False), ("staging", None, False)],
)
async def test_question_bank_seed_setting_overrides_app_env(
    monkeypatch: pytest.MonkeyPatch, app_env: str, enabled: bool | None, expected: bool
) -> None:
    """B4: a demo deployment can seed the bank without pretending to be development."""
    calls: list[str] = []

    async def noop(*_args, **_kwargs) -> bool:
        return False

    async def question_bank(*_args, **_kwargs) -> dict[str, int]:
        calls.append("question_bank")
        return {}

    monkeypatch.setattr(seeds, "seed_admin", noop)
    monkeypatch.setattr(seeds, "seed_taxonomy", noop)
    monkeypatch.setattr(seeds, "seed_question_bank", question_bank)

    settings = SimpleNamespace(app_env=app_env, question_bank_seed_enabled=enabled)
    await seeds.run_all_seeds(lambda: None, settings)

    assert (calls == ["question_bank"]) is expected


@pytest.mark.asyncio
async def test_question_bank_fixtures_fail_closed_without_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unknown environment is never assumed to be development."""
    calls: list[str] = []

    async def admin(*_args, **_kwargs) -> bool:
        calls.append("admin")
        return True

    async def taxonomy(*_args, **_kwargs) -> bool:
        calls.append("taxonomy")
        return True

    async def question_bank(*_args, **_kwargs) -> dict[str, int]:
        calls.append("question_bank")
        return {}

    monkeypatch.setattr(seeds, "seed_admin", admin)
    monkeypatch.setattr(seeds, "seed_taxonomy", taxonomy)
    monkeypatch.setattr(seeds, "seed_question_bank", question_bank)

    await seeds.run_all_seeds(lambda: None)

    assert "question_bank" not in calls
