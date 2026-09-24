import inspect
import time
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Protocol

from src.core.trace_logging import trace_event
from src.modules.job_descriptions.domain.schemas import CanonicalJobDescription
from src.modules.user_cvs.facade import DocumentArtifacts, SourceDocument


@dataclass(frozen=True)
class JobDescriptionDocument:
    upload_id: str
    filename: str
    storage_key: str
    checksum: str


@dataclass(frozen=True)
class JobDescriptionPipelineResult:
    status: str
    upload_id: str
    parser_version: str | None = None


class JobDescriptionParseRepository(Protocol):
    async def claim(self, upload_id: str) -> JobDescriptionDocument | None: ...

    async def complete(
        self,
        document: JobDescriptionDocument,
        *,
        raw_text: str,
        parsed: CanonicalJobDescription,
        parse_source: str,
    ) -> None: ...

    async def fail(self, upload_id: str, error: str) -> None: ...


class JobDescriptionObjectStorage(Protocol):
    def read(self, key: str) -> bytes: ...

    def write_json(self, key: str, payload: dict) -> None: ...


class JobDescriptionDocumentExtractor(Protocol):
    async def extract(self, document: bytes, filename: str, document_id: str) -> DocumentArtifacts: ...


class JobDescriptionParser(Protocol):
    def parse(
        self,
        source: SourceDocument,
        *,
        extraction_version: str,
        artifact_key: str | None = None,
    ) -> CanonicalJobDescription | Awaitable[CanonicalJobDescription]: ...


class JobDescriptionSourceBuilder(Protocol):
    def __call__(
        self,
        artifacts: DocumentArtifacts,
        *,
        document_id: str,
        document_sha256: str,
    ) -> SourceDocument: ...


class JobDescriptionParsingPipeline:
    """Coordinate parsing through ports, keeping Celery and infrastructure outside."""

    def __init__(
        self,
        *,
        repository: JobDescriptionParseRepository,
        storage: JobDescriptionObjectStorage,
        extractor: JobDescriptionDocumentExtractor,
        parser: JobDescriptionParser,
        source_builder: JobDescriptionSourceBuilder,
    ) -> None:
        self._repository = repository
        self._storage = storage
        self._extractor = extractor
        self._parser = parser
        self._source_builder = source_builder

    async def run(self, upload_id: str) -> JobDescriptionPipelineResult:
        started_at = time.monotonic()
        if not upload_id:
            trace_event("jd_parser", "ignored")
            return JobDescriptionPipelineResult(status="ignored", upload_id=upload_id)
        document = await self._repository.claim(upload_id)
        if document is None:
            trace_event("jd_parser", "not_claimed", upload_id=upload_id)
            return JobDescriptionPipelineResult(status="not_claimed", upload_id=upload_id)
        try:
            content = self._storage.read(document.storage_key)
            trace_event("jd_parser", "extraction_started", upload_id=document.upload_id, bytes=len(content))
            artifacts = await self._extractor.extract(content, document.filename, document.upload_id)
            if not artifacts.markdown.strip() and not artifacts.content_list:
                raise ValueError("document extractor returned no content")
            artifact_key = f"{document.storage_key}.artifacts/{document.checksum}/mineru.json"
            self._storage.write_json(artifact_key, artifacts.as_dict())
            source = self._source_builder(
                artifacts,
                document_id=document.upload_id,
                document_sha256=document.checksum,
            )
            trace_event(
                "jd_parser",
                "parser_started",
                upload_id=document.upload_id,
                extractor_version=artifacts.extractor_version,
                source_characters=len(source.text),
            )
            parsed_or_awaitable = self._parser.parse(
                source,
                extraction_version=artifacts.extractor_version or "mineru-unknown",
                artifact_key=artifact_key,
            )
            parsed = (
                await parsed_or_awaitable if inspect.isawaitable(parsed_or_awaitable) else parsed_or_awaitable
            )
            parse_source = f"mineru+{parsed.parsing.parser_version}"
            await self._repository.complete(
                document,
                raw_text=source.text,
                parsed=parsed,
                parse_source=parse_source,
            )
            trace_event(
                "jd_parser",
                "completed",
                upload_id=document.upload_id,
                parser_version=parsed.parsing.parser_version,
                duration_ms=round((time.monotonic() - started_at) * 1000),
            )
            return JobDescriptionPipelineResult(
                status="DONE",
                upload_id=upload_id,
                parser_version=parsed.parsing.parser_version,
            )
        except Exception as exc:
            await self._repository.fail(upload_id, str(exc)[:1000])
            trace_event(
                "jd_parser",
                "failed",
                upload_id=upload_id,
                error_type=type(exc).__name__,
                duration_ms=round((time.monotonic() - started_at) * 1000),
            )
            raise
