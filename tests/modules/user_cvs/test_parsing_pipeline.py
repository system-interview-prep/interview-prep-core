import json

import pytest

import src.modules.user_cvs.parsing.application.pipeline as pipeline_module
from src.modules.user_cvs.parsing.application.pipeline import CvDocument, CvParsingPipeline
from src.modules.user_cvs.parsing.domain.artifacts import DocumentArtifacts
from src.modules.user_cvs.parsing.domain.deterministic import DeterministicResumeParser
from src.modules.user_cvs.parsing.domain.source import build_source_document

SHA256 = "c" * 64


class RecordingRepository:
    def __init__(self, document: CvDocument | None) -> None:
        self.document = document
        self.completed = None
        self.failure = None
        self.released = None

    async def claim(self, cv_id: str) -> CvDocument | None:
        return self.document if self.document and self.document.cv_id == cv_id else None

    async def complete(self, document: CvDocument, **values) -> None:
        self.completed = (document, values)

    async def fail(self, cv_id: str, error: str) -> None:
        self.failure = (cv_id, error)

    async def release(self, cv_id: str) -> None:
        self.released = cv_id


class MemoryStorage:
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.json_objects = {}

    def read(self, key: str) -> bytes:
        return self.content

    def write_json(self, key: str, payload: dict) -> None:
        self.json_objects[key] = payload


class StubExtractor:
    async def extract(self, document: bytes, filename: str, document_id: str) -> DocumentArtifacts:
        assert document == b"pdf"
        assert filename == "resume.pdf"
        assert document_id == "cv-1"
        return DocumentArtifacts(
            markdown="Skills\nPython\nEnglish: B2",
            content_list=[
                {"type": "title", "text": "Skills", "page_idx": 0, "bbox": [0, 0, 50, 10]},
                {"type": "text", "text": "Python", "page_idx": 0, "bbox": [0, 20, 50, 30]},
                {"type": "text", "text": "English: B2", "page_idx": 0, "bbox": [0, 40, 80, 50]},
            ],
            extractor_version="mineru-test",
        )


async def test_pipeline_runs_upload_artifact_to_structured_persistence_boundary() -> None:
    document = CvDocument(
        cv_id="cv-1",
        filename="resume.pdf",
        storage_key="cvs/user/cv-1.pdf",
        checksum=SHA256,
    )
    repository = RecordingRepository(document)
    storage = MemoryStorage(b"pdf")
    pipeline = CvParsingPipeline(
        repository=repository,
        storage=storage,
        extractor=StubExtractor(),
        parser=DeterministicResumeParser(),
        source_builder=build_source_document,
    )

    result = await pipeline.run("cv-1")

    assert result.status == "DONE"
    assert result.canonical_status == "review_required"
    assert repository.failure is None
    assert repository.completed is not None
    _, saved = repository.completed
    persisted = json.loads(saved["parsed"].resume.model_dump_json(by_alias=True))
    assert persisted["resumeId"] == "cv-1"
    assert persisted["skills"][0]["concept"]["conceptId"] == "skill-python"
    assert persisted["languages"][0]["level"] == "B2"
    assert persisted["evidence"]
    artifact_key = f"{document.storage_key}.artifacts/{SHA256}/mineru.json"
    assert artifact_key in storage.json_objects
    assert storage.json_objects[artifact_key]["contentList"][1]["bbox"] == [0, 20, 50, 30]


async def test_pipeline_traces_quality_gate_without_document_text(monkeypatch) -> None:
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        pipeline_module,
        "trace_event",
        lambda _workflow, event, **fields: events.append((event, fields)),
    )
    document = CvDocument("cv-1", "resume.pdf", "cvs/cv-trace.pdf", SHA256)
    pipeline = CvParsingPipeline(
        repository=RecordingRepository(document),
        storage=MemoryStorage(b"pdf"),
        extractor=StubExtractor(),
        parser=DeterministicResumeParser(),
        source_builder=build_source_document,
    )

    await pipeline.run("cv-1")

    quality = next(fields for event, fields in events if event == "quality_gate")
    assert quality["status"] == "review_required"
    assert quality["coverage"] == "partial"
    assert quality["section_counts"]["evidence"] > 0
    assert "source_text" not in quality
    assert quality["raw_text_coverage"] == "complete"
    assert quality["canonical_section_coverage"]["employment"] == "complete_empty"
    assert quality["evidence_index_coverage"] == "complete"


class FailingExtractor:
    async def extract(self, document: bytes, filename: str, document_id: str) -> DocumentArtifacts:
        raise TimeoutError("extractor timeout")


async def test_pipeline_persists_failed_state_without_partial_canonical_data() -> None:
    document = CvDocument("cv-1", "resume.pdf", "cvs/cv-1.pdf", SHA256)
    repository = RecordingRepository(document)
    pipeline = CvParsingPipeline(
        repository=repository,
        storage=MemoryStorage(b"pdf"),
        extractor=FailingExtractor(),
        parser=DeterministicResumeParser(),
        source_builder=build_source_document,
    )

    with pytest.raises(TimeoutError, match="extractor timeout"):
        await pipeline.run("cv-1")
    assert repository.completed is None
    assert repository.failure == ("cv-1", "extractor timeout")


class AsyncDeterministicAdapter:
    """Proves the pipeline accepts the asynchronous hybrid-parser port."""

    def __init__(self) -> None:
        self.delegate = DeterministicResumeParser()

    async def parse(self, *args, **kwargs):
        return self.delegate.parse(*args, **kwargs)


async def test_pipeline_awaits_async_parser_and_persists_its_version() -> None:
    document = CvDocument("cv-1", "resume.pdf", "cvs/cv-1.pdf", SHA256)
    repository = RecordingRepository(document)
    pipeline = CvParsingPipeline(
        repository=repository,
        storage=MemoryStorage(b"pdf"),
        extractor=StubExtractor(),
        parser=AsyncDeterministicAdapter(),
        source_builder=build_source_document,
    )

    await pipeline.run("cv-1")

    assert repository.completed is not None
    assert repository.completed[1]["parse_source"] == "mineru+deterministic-resume-v6"


async def test_timeout_with_a_retry_left_releases_instead_of_failing() -> None:
    """The UI polls the status; a FAILED here ended its polling although the
    Celery retry then succeeded, leaving a stale error on screen."""
    document = CvDocument("cv-1", "resume.pdf", "cvs/cv-1.pdf", SHA256)
    repository = RecordingRepository(document)
    pipeline = CvParsingPipeline(
        repository=repository,
        storage=MemoryStorage(b"pdf"),
        extractor=FailingExtractor(),
        parser=DeterministicResumeParser(),
        source_builder=build_source_document,
    )

    with pytest.raises(TimeoutError):
        await pipeline.run("cv-1", final_attempt=False)
    assert repository.failure is None
    assert repository.released == "cv-1"


class TornExtractor:
    """A text layer that tore "GPA" apart and glued it to the degree line."""

    async def extract(self, document: bytes, filename: str, document_id: str) -> DocumentArtifacts:
        torn = "Bachelor of Information Technology (Data Science)GP A: 3.25/4.00"
        return DocumentArtifacts(
            markdown=f"Education\n{torn}\nSkills\nPython",
            content_list=[
                {"type": "title", "text": "Education", "page_idx": 0, "bbox": [0, 0, 50, 10]},
                {"type": "text", "text": torn, "page_idx": 0, "bbox": [0, 20, 50, 30]},
                {"type": "title", "text": "Skills", "page_idx": 0, "bbox": [0, 40, 50, 50]},
                {"type": "text", "text": "Python", "page_idx": 0, "bbox": [0, 60, 50, 70]},
            ],
            extractor_version="pdf_text-test",
        )


class RepairScript:
    def complete(self, *, instructions: str, input_text: str, output_schema: dict) -> str:
        blocks = json.loads(input_text)["blocks"]
        fixed = [b.replace("Science)GP A:", "Science)\nGPA:") for b in blocks]
        return json.dumps({"blocks": fixed}, ensure_ascii=False)


async def test_repair_runs_before_the_source_and_is_recorded_in_parse_source() -> None:
    from src.modules.user_cvs.parsing.domain.text_repair import TextRepairer

    document = CvDocument(cv_id="cv-1", filename="resume.pdf", storage_key="cvs/u/cv-1.pdf", checksum=SHA256)
    repository = RecordingRepository(document)
    storage = MemoryStorage(b"pdf")
    pipeline = CvParsingPipeline(
        repository=repository,
        storage=storage,
        extractor=TornExtractor(),
        parser=DeterministicResumeParser(),
        source_builder=build_source_document,
        repairer=TextRepairer(RepairScript()),
    )

    result = await pipeline.run("cv-1")

    assert result.status == "DONE"
    _, saved = repository.completed
    # The raw text people see, and every evidence offset, come from the repaired copy.
    assert "GPA: 3.25/4.00" in saved["raw_text"]
    assert "GP A" not in saved["raw_text"]
    assert saved["parse_source"] == "mineru+repair-v1+deterministic-resume-v6"
    # The stored artifact is still the extractor's own output.
    artifact = storage.json_objects[f"{document.storage_key}.artifacts/{SHA256}/mineru.json"]
    assert "GP A" in artifact["contentList"][1]["text"]


async def test_without_a_repairer_the_pipeline_is_unchanged() -> None:
    document = CvDocument(cv_id="cv-1", filename="resume.pdf", storage_key="cvs/u/cv-1.pdf", checksum=SHA256)
    repository = RecordingRepository(document)
    pipeline = CvParsingPipeline(
        repository=repository,
        storage=MemoryStorage(b"pdf"),
        extractor=TornExtractor(),
        parser=DeterministicResumeParser(),
        source_builder=build_source_document,
    )

    await pipeline.run("cv-1")

    _, saved = repository.completed
    assert "GP A: 3.25/4.00" in saved["raw_text"]
    assert saved["parse_source"] == "mineru+deterministic-resume-v6"
