from types import SimpleNamespace

import pytest

from src.workers.tasks import cv


class Database:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None


class RecordingPipeline:
    created_with = None

    def __init__(self, **dependencies):
        RecordingPipeline.created_with = dependencies

    async def run(self, cv_id):
        return SimpleNamespace(status="DONE", cv_id=cv_id, canonical_status="review_required")


@pytest.mark.parametrize("mode", ["deterministic", "hybrid"])
async def test_cv_worker_selects_configured_parser(monkeypatch, mode) -> None:
    async def load_taxonomy(_):
        return SimpleNamespace(skills={}, version="taxonomy-v1")

    monkeypatch.setattr(cv, "SessionFactory", Database)
    monkeypatch.setattr(cv, "load_active_skill_taxonomy", load_taxonomy)
    monkeypatch.setattr(cv, "get_settings", lambda: SimpleNamespace(cv_parser_mode=mode))
    monkeypatch.setattr(cv, "CvParsingPipeline", RecordingPipeline)

    result = await cv._parse_cv("cv-1")

    assert result == {"status": "DONE", "cv_id": "cv-1", "canonical_status": "review_required"}
    assert RecordingPipeline.created_with["parser"].__class__.__name__.casefold().startswith(mode)


async def test_cv_worker_rejects_unknown_parser_mode(monkeypatch) -> None:
    async def load_taxonomy(_):
        return SimpleNamespace(skills={}, version="taxonomy-v1")

    monkeypatch.setattr(cv, "SessionFactory", Database)
    monkeypatch.setattr(cv, "load_active_skill_taxonomy", load_taxonomy)
    monkeypatch.setattr(cv, "get_settings", lambda: SimpleNamespace(cv_parser_mode="unknown"))

    with pytest.raises(ValueError, match="CV_PARSER_MODE"):
        await cv._parse_cv("cv-1")
