import json

import pytest

from src.modules.job_descriptions.parsing.application.pipeline import (
    JobDescriptionDocument,
    JobDescriptionParsingPipeline,
)
from src.modules.job_descriptions.parsing.deterministic import DeterministicJobDescriptionParser
from src.modules.user_cvs.parsing.domain.artifacts import DocumentArtifacts
from src.modules.user_cvs.parsing.domain.source import build_source_document


class Repository:
    def __init__(self) -> None:
        self.document = JobDescriptionDocument("jd-1", "jd.pdf", "jd/jd-1.pdf", "a" * 64)
        self.completed = None
        self.failure = None

    async def claim(self, upload_id):
        return self.document if upload_id == "jd-1" else None

    async def complete(self, document, **values):
        self.completed = (document, values)

    async def fail(self, upload_id, error):
        self.failure = (upload_id, error)


class Storage:
    def __init__(self):
        self.writes = {}

    def read(self, key):
        return b"%PDF-1.7"

    def write_json(self, key, payload):
        self.writes[key] = payload


class Extractor:
    async def extract(self, document, filename, document_id):
        return DocumentArtifacts(
            markdown="Job Title: Backend Engineer\nRequirements\n- Python",
            content_list=[
                {"type": "text", "text": "Job Title: Backend Engineer"},
                {"type": "text", "text": "Requirements"},
                {"type": "text", "text": "- Python"},
            ],
            extractor_version="mineru-test",
        )


async def test_jd_pipeline_persists_artifact_and_canonical_result() -> None:
    repository, storage = Repository(), Storage()
    pipeline = JobDescriptionParsingPipeline(
        repository=repository,
        storage=storage,
        extractor=Extractor(),
        parser=DeterministicJobDescriptionParser(),
        source_builder=build_source_document,
    )

    result = await pipeline.run("jd-1")

    assert result.status == "DONE"
    assert result.parser_version == "deterministic-jd-v4"
    assert repository.completed is not None
    persisted = json.loads(repository.completed[1]["parsed"].model_dump_json(by_alias=True))
    assert persisted["jobTitle"] == "Backend Engineer"
    assert repository.completed[1]["parse_source"] == "mineru+deterministic-jd-v4"
    assert next(iter(storage.writes)).endswith("/mineru.json")


class FailingExtractor:
    async def extract(self, *_):
        raise TimeoutError("temporary extractor outage")


async def test_jd_pipeline_records_failure_and_reraises_for_celery_retry() -> None:
    repository = Repository()
    pipeline = JobDescriptionParsingPipeline(
        repository=repository,
        storage=Storage(),
        extractor=FailingExtractor(),
        parser=DeterministicJobDescriptionParser(),
        source_builder=build_source_document,
    )
    with pytest.raises(TimeoutError):
        await pipeline.run("jd-1")
    assert repository.failure == ("jd-1", "temporary extractor outage")


async def test_jd_pipeline_does_not_process_unclaimed_upload() -> None:
    result = await JobDescriptionParsingPipeline(
        repository=Repository(),
        storage=Storage(),
        extractor=Extractor(),
        parser=DeterministicJobDescriptionParser(),
        source_builder=build_source_document,
    ).run("missing")
    assert result.status == "not_claimed"
