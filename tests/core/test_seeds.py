from types import SimpleNamespace

import pytest

import src.seeds as seeds


@pytest.fixture(autouse=True)
def _job_description_seed(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The sample-job seed needs a database; record its calls instead."""
    calls: list[str] = []

    async def job_descriptions(*_args, **_kwargs) -> dict[str, int]:
        calls.append("job_descriptions")
        return {}

    monkeypatch.setattr(seeds, "seed_job_descriptions", job_descriptions)
    return calls


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


@pytest.mark.asyncio
@pytest.mark.parametrize(("app_env", "expected"), [("development", True), ("production", False)])
async def test_sample_jobs_are_seeded_only_with_dev_fixtures(
    monkeypatch: pytest.MonkeyPatch, _job_description_seed: list[str], app_env: str, expected: bool
) -> None:
    async def noop(*_args, **_kwargs) -> dict[str, int]:
        return {}

    monkeypatch.setattr(seeds, "seed_admin", noop)
    monkeypatch.setattr(seeds, "seed_taxonomy", noop)
    monkeypatch.setattr(seeds, "seed_question_bank", noop)

    await seeds.run_all_seeds(lambda: None, _settings(app_env))

    assert (_job_description_seed == ["job_descriptions"]) is expected


def test_sample_job_fixtures_are_well_formed() -> None:
    from src.seeds.job_description_seed import load_job_description_fixtures

    jobs = load_job_description_fixtures()

    assert len(jobs) >= 20
    assert len({job.external_job_id for job in jobs}) == len(jobs)
    for job in jobs:
        assert job.title and job.company_name and job.source_url
        assert "Yêu cầu ứng viên" in job.raw_html
