from types import SimpleNamespace

import pytest

from src.workers.tasks import job_description


class Database:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None


class RecordingPipeline:
    created_with = None

    def __init__(self, **dependencies):
        RecordingPipeline.created_with = dependencies

    async def run(self, upload_id):
        return SimpleNamespace(status="DONE", upload_id=upload_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["deterministic", "hybrid"])
async def test_worker_selects_configured_parser_and_delegates_to_pipeline(monkeypatch, mode) -> None:
    taxonomy = SimpleNamespace(skills={}, version="taxonomy-v1")

    async def load_taxonomy(_):
        return taxonomy

    monkeypatch.setattr(job_description, "SessionFactory", Database)
    monkeypatch.setattr(job_description, "load_active_skill_taxonomy", load_taxonomy)
    monkeypatch.setattr(job_description, "get_settings", lambda: SimpleNamespace(jd_parser_mode=mode))
    monkeypatch.setattr(job_description, "JobDescriptionParsingPipeline", RecordingPipeline)

    result = await job_description._parse_job_description("up-1")

    assert result == {"status": "DONE", "upload_id": "up-1"}
    parser = RecordingPipeline.created_with["parser"]
    assert parser.__class__.__name__.casefold().startswith(mode)


@pytest.mark.asyncio
async def test_worker_rejects_unknown_parser_mode(monkeypatch) -> None:
    async def load_taxonomy(_):
        return SimpleNamespace(skills={}, version="taxonomy-v1")

    monkeypatch.setattr(job_description, "SessionFactory", Database)
    monkeypatch.setattr(job_description, "load_active_skill_taxonomy", load_taxonomy)
    monkeypatch.setattr(job_description, "get_settings", lambda: SimpleNamespace(jd_parser_mode="unsafe"))
    with pytest.raises(ValueError, match="JD_PARSER_MODE"):
        await job_description._parse_job_description("up-1")


@pytest.mark.asyncio
async def test_parse_job_description_ignores_empty_upload_id() -> None:
    assert await job_description._parse_job_description("") == {"status": "ignored"}
