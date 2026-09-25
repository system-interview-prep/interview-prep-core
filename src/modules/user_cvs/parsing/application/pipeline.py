import inspect
import time
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Protocol

from src.core.trace_logging import trace_event
from src.modules.user_cvs.domain.schemas import ParsedResume
from src.modules.user_cvs.parsing.domain.artifacts import DocumentArtifacts
from src.modules.user_cvs.parsing.domain.source import SourceDocument


def _coverage_snapshot(parsed: ParsedResume, source: SourceDocument) -> dict:
    """Compute independent source, canonical-section and evidence-index coverage.

    Empty employment is not a parser failure when the source is complete: it is
    represented as ``complete_empty`` so matching can safely distinguish an
    explicit absence from an unreadable section.
    """
    resume = parsed.resume
    metadata = resume.parsing
    warnings = metadata.warnings if metadata else []
    raw_coverage = "complete" if source.text.strip() else "unavailable"
    if any(item.severity == "error" for item in warnings):
        raw_coverage = "partial" if source.text.strip() else "unavailable"

    section_values = {
        "profile": bool(resume.profile.headline or resume.profile.summary),
        "skills": bool(resume.skills),
        "employment": bool(resume.employment),
        "education": bool(resume.education),
        "projects": bool(resume.projects),
        "certifications": bool(resume.certifications),
        "languages": bool(resume.languages),
        "career_classifications": bool(resume.career_classifications),
    }
    section_coverage = {
        name: (
            "complete"
            if present
            else (
                "partial"
                if raw_coverage != "complete"
                or any(block.section == name for block in source.blocks)
                else "complete_empty"
            )
        )
        for name, present in section_values.items()
    }
    evidence_valid = bool(resume.evidence) and all(
        item.document_id == resume.document_id
        and item.document_sha256 == resume.document_sha256
        and 0 <= item.char_start < item.char_end <= len(source.text)
        and source.text[item.char_start : item.char_end] == item.text
        for item in resume.evidence
    )
    evidence_coverage = "complete" if evidence_valid else "partial" if resume.evidence else "unavailable"
    reasons: list[str] = []
    if raw_coverage != "complete":
        reasons.append("raw_text_incomplete")
    if any(value == "partial" for value in section_coverage.values()):
        reasons.append("canonical_sections_incomplete")
    if evidence_coverage != "complete":
        reasons.append("evidence_index_incomplete")
    return {
        "raw_text_coverage": raw_coverage,
        "canonical_section_coverage": section_coverage,
        "evidence_index_coverage": evidence_coverage,
        "coverage_reason_codes": reasons,
        "section_counts": {
            "skills": len(resume.skills),
            "employment": len(resume.employment),
            "education": len(resume.education),
            "projects": len(resume.projects),
            "certifications": len(resume.certifications),
            "languages": len(resume.languages),
            "evidence": len(resume.evidence),
        },
    }


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
        started_at = time.monotonic()
        if not cv_id:
            trace_event("cv_parser", "ignored")
            return PipelineResult(status="ignored", cv_id=cv_id)
        document = await self._repository.claim(cv_id)
        if document is None:
            trace_event("cv_parser", "not_claimed", cv_id=cv_id)
            return PipelineResult(status="not_claimed", cv_id=cv_id)
        try:
            content = self._storage.read(document.storage_key)
            trace_event("cv_parser", "extraction_started", cv_id=document.cv_id, bytes=len(content))
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
            trace_event(
                "cv_parser",
                "parser_started",
                cv_id=document.cv_id,
                extractor_version=artifacts.extractor_version,
                source_characters=len(source.text),
            )
            parsed_or_awaitable = self._parser.parse(
                source,
                extraction_version=artifacts.extractor_version or "mineru-unknown",
                source_artifact_key=artifact_key,
            )
            parsed = (
                await parsed_or_awaitable if inspect.isawaitable(parsed_or_awaitable) else parsed_or_awaitable
            )
            coverage = _coverage_snapshot(parsed, source)
            if parsed.resume.parsing:
                parsing = parsed.resume.parsing.model_copy(
                    update={
                        key: coverage[key]
                        for key in (
                            "raw_text_coverage",
                            "canonical_section_coverage",
                            "evidence_index_coverage",
                            "coverage_reason_codes",
                        )
                    }
                )
                parsed = parsed.model_copy(
                    update={"resume": parsed.resume.model_copy(update={"parsing": parsing})}
                )
            parser_version = parsed.resume.parsing.parser_version if parsed.resume.parsing else "unknown"
            trace_event(
                "cv_parser",
                "quality_gate",
                cv_id=document.cv_id,
                status=parsed.resume.parsing.status if parsed.resume.parsing else "review_required",
                parser_version=parser_version,
                extraction_version=artifacts.extractor_version or "unknown",
                source_characters=len(source.text),
                section_counts=coverage["section_counts"],
                warnings_count=len(parsed.resume.parsing.warnings) if parsed.resume.parsing else 0,
                checks={
                    "source_text_present": bool(source.text.strip()),
                    "parsing_metadata_present": parsed.resume.parsing is not None,
                    "evidence_present": bool(parsed.resume.evidence),
                    "parser_warnings_empty": (
                        not parsed.resume.parsing.warnings if parsed.resume.parsing else False
                    ),
                    "document_checksum_matches": source.document_sha256 == document.checksum,
                    "artifact_persisted": True,
                },
                coverage=(
                    "partial"
                    if parsed.resume.parsing and parsed.resume.parsing.status == "review_required"
                    else "complete"
                ),
                raw_text_coverage=coverage["raw_text_coverage"],
                canonical_section_coverage=coverage["canonical_section_coverage"],
                evidence_index_coverage=coverage["evidence_index_coverage"],
                coverage_reason_codes=coverage["coverage_reason_codes"],
            )
            await self._repository.complete(
                document,
                raw_text=source.text,
                parsed=parsed,
                parse_source=f"mineru+{parser_version}",
            )
            canonical_status = parsed.resume.parsing.status if parsed.resume.parsing else None
            trace_event(
                "cv_parser",
                "completed",
                cv_id=document.cv_id,
                canonical_status=canonical_status,
                parser_version=parser_version,
                duration_ms=round((time.monotonic() - started_at) * 1000),
            )
            return PipelineResult(
                status="DONE",
                cv_id=document.cv_id,
                canonical_status=canonical_status,
            )
        except Exception as exc:
            await self._repository.fail(document.cv_id, str(exc)[:1000])
            trace_event(
                "cv_parser",
                "failed",
                cv_id=document.cv_id,
                error_type=type(exc).__name__,
                duration_ms=round((time.monotonic() - started_at) * 1000),
            )
            raise
