import inspect
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Protocol

from src.modules.user_cvs.domain.schemas import ParsedResume
from src.modules.user_cvs.parsing.domain.artifacts import DocumentArtifacts
from src.modules.user_cvs.parsing.domain.source import SourceDocument


@dataclass(frozen=True)
class CvDocument:
    cv_id: str
    filename: str
    storage_key: str
    checksum: str


@dataclass(frozen=True)
class PipelineResult:
    status: str
    cv_id: str
    canonical_status: str | None = None


class CvParseRepository(Protocol):
    async def claim(self, cv_id: str) -> CvDocument | None: ...

    async def complete(
        self,
        document: CvDocument,
        *,
        raw_text: str,
        parsed: ParsedResume,
        parse_source: str,
    ) -> None: ...

    async def fail(self, cv_id: str, error: str) -> None: ...


class ObjectStorage(Protocol):
    def read(self, key: str) -> bytes: ...

    def write_json(self, key: str, payload: dict) -> None: ...


class DocumentExtractor(Protocol):
    async def extract(self, document: bytes, filename: str, document_id: str) -> DocumentArtifacts: ...


class ResumeParser(Protocol):
    def parse(
        self,
        source: SourceDocument,
        *,
        extraction_version: str,
        source_artifact_key: str | None = None,
    ) -> ParsedResume | Awaitable[ParsedResume]: ...


class SourceBuilder(Protocol):
    def __call__(
        self,
        artifacts: DocumentArtifacts,
        *,
        document_id: str,
        document_sha256: str,
    ) -> SourceDocument: ...


class CvParsingPipeline:
    """Application service coordinating ports; infrastructure stays outside."""

    def __init__(
        self,
        *,
        repository: CvParseRepository,
        storage: ObjectStorage,
        extractor: DocumentExtractor,
        parser: ResumeParser,
        source_builder: SourceBuilder,
    ) -> None:
        self._repository = repository
        self._storage = storage
        self._extractor = extractor
        self._parser = parser
        self._source_builder = source_builder

    async def run(self, cv_id: str) -> PipelineResult:
        if not cv_id:
            return PipelineResult(status="ignored", cv_id=cv_id)
        document = await self._repository.claim(cv_id)
        if document is None:
            return PipelineResult(status="not_claimed", cv_id=cv_id)
        try:
            content = self._storage.read(document.storage_key)
            artifacts = await self._extractor.extract(content, document.filename, document.cv_id)
            if not artifacts.markdown.strip() and not artifacts.content_list:
                raise ValueError("document extractor returned no content")

            artifact_key = f"{document.storage_key}.artifacts/{document.checksum}/mineru.json"
            self._storage.write_json(artifact_key, artifacts.as_dict())
            source = self._source_builder(
                artifacts,
                document_id=document.cv_id,
                document_sha256=document.checksum,
            )
            parsed_or_awaitable = self._parser.parse(
                source,
                extraction_version=artifacts.extractor_version or "mineru-unknown",
                source_artifact_key=artifact_key,
            )
            parsed = (
                await parsed_or_awaitable if inspect.isawaitable(parsed_or_awaitable) else parsed_or_awaitable
            )
            parser_version = parsed.resume.parsing.parser_version if parsed.resume.parsing else "unknown"
            await self._repository.complete(
                document,
                raw_text=source.text,
                parsed=parsed,
                parse_source=f"mineru+{parser_version}",
            )
            canonical_status = parsed.resume.parsing.status if parsed.resume.parsing else None
            return PipelineResult(
                status="DONE",
                cv_id=document.cv_id,
                canonical_status=canonical_status,
            )
        except Exception as exc:
            await self._repository.fail(document.cv_id, str(exc)[:1000])
            raise
