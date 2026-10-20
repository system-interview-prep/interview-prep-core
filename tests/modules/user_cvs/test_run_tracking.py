"""CV upload and parse attempts emit the admin CV-parsing events, and tracking never breaks them."""

import json
from types import SimpleNamespace

import pytest

import src.modules.user_cvs.parsing.application.pipeline as pipeline_module
from src.core import pipeline_events as pe
from src.modules.user_cvs.application import run_tracking
from src.modules.user_cvs.application.review_service import CvReviewConflictError, CvReviewService
from src.modules.user_cvs.application.upload_service import CvQueueUnavailable, CvUploadService
from src.modules.user_cvs.domain.schemas import ParserWarning
from src.modules.user_cvs.parsing.application.pipeline import CvDocument, CvParsingPipeline
from src.modules.user_cvs.parsing.domain.artifacts import DocumentArtifacts
from src.modules.user_cvs.parsing.domain.deterministic import DeterministicResumeParser
from src.modules.user_cvs.parsing.domain.source import build_source_document
from src.workers.tasks import cv as cv_task

SHA256 = "c" * 64
CV_TEXT = "Skills\nPython\nEnglish: B2"


@pytest.fixture
def recorder(monkeypatch):
    monkeypatch.setattr(pe, "enabled", lambda: True)
    monkeypatch.setattr(pe, "_ensure_thread", lambda: None)
    buffers = (pe._recorder.events, pe._recorder.calls, pe._recorder.cv_events, pe._recorder.cv_calls)
    for buffer in buffers:
        buffer.clear()
    token = pe.current_cv_run.set(None)
    yield pe._recorder
    pe.current_cv_run.reset(token)
    for buffer in buffers:
        buffer.clear()


def _rows(recorder):
    return [{**row, "payload": json.loads(row["payload"])} for row in recorder.cv_events]


def _all_payload_text(recorder) -> str:
    return " ".join(row["payload"] for row in recorder.cv_events)


# --------------------------------------------------------------------------- upload


class UploadRepository:
    def __init__(self, existing_id=None):
        self.existing_id = existing_id
        self.created = None
        self.dispatch_failure = None

    async def find_id_by_checksum(self, user_id, checksum):
        return self.existing_id

    async def filenames(self, user_id):
        return set(getattr(self, "names", set()))

    async def create(self, record):
        self.created = record

    async def mark_dispatch_failed(self, cv_id, error):
        self.dispatch_failure = (cv_id, error)


class FileStorage:
    def write(self, key, content, content_type):
        pass

    def delete(self, key):
        pass

    def url(self, key, cv_id):
        return f"https://storage.test/{key}"


class Publisher:
    def __init__(self, error=None):
        self.error = error

    def publish(self, cv_id):
        if self.error:
            raise self.error


def _upload_service(repository, publisher=None):
    return CvUploadService(repository=repository, storage=FileStorage(), publisher=publisher or Publisher())


async def _upload(service):
    return await service.upload(
        user_id="user-1", filename="Nguyen Van A.pdf", content_type="application/pdf", content=b"%PDF-1.7"
    )


async def test_upload_emits_uploaded_and_enqueued(recorder):
    repository = UploadRepository()
    result = await _upload(_upload_service(repository))

    rows = _rows(recorder)
    assert [(r["stage"], r["event"], r["status"]) for r in rows] == [
        ("upload", "cv_uploaded", "completed"),
        ("queue", "cv_enqueued", "info"),
    ]
    assert {(r["cv_id"], r["user_id"]) for r in rows} == {(result.cv_id, "user-1")}
    assert rows[0]["attempt_id"] == rows[1]["attempt_id"]
    uploaded = rows[0]["payload"]
    assert {k: uploaded[k] for k in ("extension", "content_type", "size_bytes", "duplicate")} == {
        "extension": "pdf",
        "content_type": "application/pdf",
        "size_bytes": 8,
        "duplicate": False,
    }
    assert isinstance(uploaded["duration_ms"], int)
    assert rows[1]["payload"] == {"reparse": False}
    assert "Nguyen" not in _all_payload_text(recorder)
    assert pe.current_cv_run.get() is None


async def test_duplicate_upload_attaches_one_event_to_the_existing_cv(recorder):
    repository = UploadRepository(existing_id="cv-old")
    result = await _upload(_upload_service(repository))

    assert result.cv_id == "cv-old" and result.created is False and repository.created is None
    (row,) = _rows(recorder)
    assert (row["cv_id"], row["stage"], row["event"], row["status"]) == (
        "cv-old",
        "upload",
        "cv_upload_duplicate",
        "info",
    )
    assert row["payload"]["duplicate"] is True


async def test_dispatch_failure_is_recorded_without_the_error_text(recorder):
    repository = UploadRepository()
    service = _upload_service(repository, Publisher(RuntimeError("amqp://user:secret@host refused")))
    with pytest.raises(CvQueueUnavailable):
        await _upload(service)

    rows = _rows(recorder)
    assert [r["event"] for r in rows] == ["cv_uploaded", "cv_dispatch_failed"]
    assert rows[1]["status"] == "error"
    assert rows[1]["payload"] == {"error_type": "RuntimeError", "reason": "dispatch_failed"}
    assert "secret" not in _all_payload_text(recorder)


async def test_upload_never_fails_because_of_tracking(recorder, monkeypatch):
    def broken(*_args, **_kwargs):
        raise RuntimeError("tracking down")

    monkeypatch.setattr(run_tracking, "trace_event", broken)
    result = await _upload(_upload_service(UploadRepository()))
    assert result.created is True


class ReviewRepository:
    async def queue_reparse_owned(self, user_id, cv_id):
        return True

    async def mark_dispatch_failed(self, cv_id, error):
        self.failed = cv_id


async def test_reparse_emits_an_enqueue_flagged_as_reparse(recorder):
    await CvReviewService(ReviewRepository(), Publisher()).reparse("user-1", "cv-1")
    (row,) = _rows(recorder)
    assert (row["cv_id"], row["event"], row["payload"]) == ("cv-1", "cv_enqueued", {"reparse": True})

    recorder.cv_events.clear()
    with pytest.raises(CvReviewConflictError):
        await CvReviewService(ReviewRepository(), Publisher(RuntimeError("down"))).reparse("user-1", "cv-1")
    assert [r["event"] for r in _rows(recorder)] == ["cv_dispatch_failed"]


# --------------------------------------------------------------------------- parse


class ParseRepository:
    def __init__(self, document):
        self.document = document
        self.completed = None
        self.failure = None
        self.released = None

    async def claim(self, cv_id):
        return self.document if self.document and self.document.cv_id == cv_id else None

    async def complete(self, document, **values):
        self.completed = (document, values)

    async def fail(self, cv_id, error):
        self.failure = (cv_id, error)

    async def release(self, cv_id):
        self.released = cv_id


class Storage:
    def read(self, key):
        return b"pdf"

    def write_json(self, key, payload):
        pass


class Extractor:
    provider_name = "pdf_text"

    async def extract(self, document, filename, document_id):
        return DocumentArtifacts(
            markdown=CV_TEXT,
            content_list=[
                {"type": "title", "text": "Skills", "page_idx": 0, "bbox": [0, 0, 50, 10]},
                {"type": "text", "text": "Python", "page_idx": 0, "bbox": [0, 20, 50, 30]},
                {"type": "text", "text": "English: B2", "page_idx": 0, "bbox": [0, 40, 80, 50]},
            ],
            extractor_version="pdf-text-v1",
        )


class FailingExtractor:
    def __init__(self, error):
        self.error = error

    async def extract(self, document, filename, document_id):
        raise self.error


class LlmParser:
    """A parser that makes one (recorded) model call, like the hybrid parser."""

    def __init__(self):
        self.delegate = DeterministicResumeParser()

    async def parse(self, *args, **kwargs):
        pe.record_ai_call(
            purpose="user_cvs.hybrid.HybridResumeParser.parse",
            provider="openai",
            model="m",
            status="ok",
            duration_ms=5,
        )
        parsed = self.delegate.parse(*args, **kwargs)
        parsing = parsed.resume.parsing
        warning = ParserWarning(
            code="llm_fallback", path="employment[0].title", message="LLM rejected 'Nguyen Van A'"
        )
        parsing = parsing.model_copy(update={"warnings": [*parsing.warnings, warning]})
        return parsed.model_copy(update={"resume": parsed.resume.model_copy(update={"parsing": parsing})})


def _document():
    return CvDocument("cv-1", "Nguyen Van A.pdf", "cvs/user-1/cv-1.pdf", SHA256, user_id="user-1")


def _pipeline(repository, *, extractor=None, parser=None):
    return CvParsingPipeline(
        repository=repository,
        storage=Storage(),
        extractor=extractor or Extractor(),
        parser=parser or DeterministicResumeParser(),
        source_builder=build_source_document,
    )


async def test_successful_parse_emits_every_step(recorder):
    repository = ParseRepository(_document())
    result = await _pipeline(repository, parser=LlmParser()).run("cv-1")

    assert result.status == "DONE"
    rows = _rows(recorder)
    assert [(r["stage"], r["event"]) for r in rows] == [
        ("queue", "cv_parse_claimed"),
        ("extract", "extraction_started"),
        ("extract", "extraction_completed"),
        ("source", "source_built"),
        ("parse", "parser_started"),
        ("parse", "parser_completed"),
        ("quality", "quality_gate"),
        ("save", "cv_saved"),
        ("done", "completed"),
    ]
    assert len({r["attempt_id"] for r in rows}) == 1
    assert {(r["cv_id"], r["user_id"]) for r in rows} == {("cv-1", "user-1")}
    by_event = {r["event"]: r for r in rows}
    extraction = by_event["extraction_completed"]["payload"]
    assert (extraction["provider"], extraction["extractor_version"]) == ("pdf_text", "pdf-text-v1")
    assert (extraction["markdown_characters"], extraction["content_blocks"]) == (len(CV_TEXT), 3)
    source = by_event["source_built"]["payload"]
    assert source["source_characters"] > 0 and source["block_count"] > 0
    parsed = by_event["parser_completed"]["payload"]
    assert parsed["llm_used"] is True and parsed["llm_calls"] == 1
    assert parsed["skills_count"] >= 1 and parsed["evidence_count"] >= 1
    assert parsed["parser_version"]
    gate = by_event["quality_gate"]
    assert gate["status"] == "warning"  # review_required
    assert {"code": "llm_fallback", "severity": "warning", "section": "employment"} in gate["payload"][
        "warnings"
    ]
    assert by_event["completed"]["status"] == "completed"
    assert isinstance(by_event["cv_saved"]["duration_ms"], int)
    # The CV text, file name and warning messages never reach the payloads.
    text_ = _all_payload_text(recorder)
    assert "Nguyen" not in text_ and "English: B2" not in text_ and "LLM rejected" not in text_
    # The model call carries the CV id.
    assert [c["cv_id"] for c in recorder.cv_calls] == ["cv-1"]
    assert pe.current_cv_run.get() is None


async def test_failed_parse_records_a_safe_reason_not_the_message(recorder):
    repository = ParseRepository(_document())
    error = ValueError("could not read 'Nguyen Van A, an@example.com'")
    with pytest.raises(ValueError):
        await _pipeline(repository, extractor=FailingExtractor(error)).run("cv-1")

    rows = _rows(recorder)
    assert [r["event"] for r in rows] == ["cv_parse_claimed", "extraction_started", "failed"]
    failed = rows[-1]
    assert failed["status"] == "error"
    assert {k: failed["payload"][k] for k in ("error_type", "failed_stage", "reason")} == {
        "error_type": "ValueError",
        "failed_stage": "extract",
        "reason": "extraction_failed",
    }
    assert "Nguyen" not in _all_payload_text(recorder)
    assert repository.failure is not None  # the CV row still gets the full error


async def test_timeout_with_retry_left_records_retry_scheduled(recorder):
    repository = ParseRepository(_document())
    with pytest.raises(TimeoutError):
        await _pipeline(repository, extractor=FailingExtractor(TimeoutError("slow 'Nguyen'"))).run(
            "cv-1", final_attempt=False
        )
    retry = _rows(recorder)[-1]
    assert (retry["stage"], retry["event"], retry["status"]) == ("done", "retry_scheduled", "warning")
    assert "error" not in retry["payload"] and retry["payload"]["error_type"] == "TimeoutError"
    assert retry["payload"]["reason"] == "timeout"


async def test_empty_extraction_has_its_own_reason(recorder):
    class Empty:
        async def extract(self, *_args):
            return DocumentArtifacts(markdown="  ", content_list=[])

    with pytest.raises(ValueError, match="no content"):
        await _pipeline(ParseRepository(_document()), extractor=Empty()).run("cv-1")
    assert _rows(recorder)[-1]["payload"]["reason"] == "empty_extraction"


async def test_not_claimed_is_recorded_as_skipped(recorder):
    result = await _pipeline(ParseRepository(None)).run("cv-9")
    assert result.status == "not_claimed"
    (row,) = _rows(recorder)
    assert (row["cv_id"], row["stage"], row["event"], row["status"]) == (
        "cv-9",
        "queue",
        "not_claimed",
        "skipped",
    )


async def test_parsing_never_fails_because_of_tracking(recorder, monkeypatch):
    def broken(*_args, **_kwargs):
        raise RuntimeError("tracking down")

    monkeypatch.setattr(pipeline_module, "trace_event", broken)
    monkeypatch.setattr(pe, "cv_payload", broken)
    repository = ParseRepository(_document())
    result = await _pipeline(repository).run("cv-1")
    assert result.status == "DONE" and repository.completed is not None


async def test_recording_failures_inside_the_buffer_are_swallowed(recorder, monkeypatch):
    monkeypatch.setattr(pe, "cv_payload", lambda _fields: (_ for _ in ()).throw(RuntimeError("bad")))
    result = await _pipeline(ParseRepository(_document())).run("cv-1")
    assert result.status == "DONE" and not recorder.cv_events


# --------------------------------------------------------------------------- worker


class Database:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None


async def test_worker_opens_the_attempt_with_its_number_and_parser_mode(recorder, monkeypatch):
    async def load_taxonomy(_):
        return SimpleNamespace(skills={}, version="taxonomy-v1")

    repository = ParseRepository(_document())
    monkeypatch.setattr(cv_task, "SessionFactory", Database)
    monkeypatch.setattr(cv_task, "load_active_skill_taxonomy", load_taxonomy)
    monkeypatch.setattr(cv_task, "get_settings", lambda: SimpleNamespace(cv_parser_mode="deterministic"))
    monkeypatch.setattr(cv_task, "SqlAlchemyCvParseRepository", lambda _session: repository)
    monkeypatch.setattr(cv_task, "R2ObjectStorage", Storage)
    monkeypatch.setattr(cv_task, "build_document_extractor", Extractor)

    result = await cv_task._parse_cv("cv-1", attempt=2)

    assert result["status"] == "DONE"
    rows = _rows(recorder)
    claimed = rows[0]
    assert claimed["event"] == "cv_parse_claimed"
    assert claimed["payload"]["attempt"] == 2 and claimed["payload"]["parser_mode"] == "deterministic"
    assert len({r["attempt_id"] for r in rows}) == 1  # the pipeline reused the worker's attempt
    parsed = next(r for r in rows if r["event"] == "parser_completed")["payload"]
    assert parsed["parser_mode"] == "deterministic" and parsed["llm_used"] is False


# --------------------------------------------------------------------------- helpers


def test_helpers_keep_codes_only():
    assert run_tracking.file_extension("Nguyen Van A.PDF") == "pdf"
    assert run_tracking.file_extension("") is None
    assert run_tracking.warning_section("employment[0].startDate") == "employment"
    assert run_tracking.warning_section("skills") == "skills"
    assert run_tracking.warning_section("free text with spaces") is None
    items = run_tracking.warning_items(
        [{"code": "x", "severity": "info", "path": "education", "message": "secret"}] * 50
    )
    assert len(items) == 40 and items[0] == {"code": "x", "severity": "info", "section": "education"}
    assert run_tracking.failure_reason(TimeoutError(), "extract") == "timeout"
    assert run_tracking.failure_reason(KeyError("cv text"), "parse") == "parser_failed"
    assert run_tracking.failure_reason(KeyError("cv text"), None) == "unexpected_error"
