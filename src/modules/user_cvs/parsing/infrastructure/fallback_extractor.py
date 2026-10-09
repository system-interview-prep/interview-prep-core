"""Try document extractors in order until one returns content.

MinerU outages (transport errors, timeouts, API errors) used to fail every CV
and JD upload even though PaddleOCR was configured. This adapter keeps the
pipelines unaware of providers: it exposes the same ``extract`` port and sets
``provider_name`` to whichever provider produced the artifacts, so the stored
artifact key names the real source.
"""

from __future__ import annotations

from src.core.trace_logging import trace_event
from src.modules.user_cvs.parsing.domain.artifacts import DocumentArtifacts


class AllExtractorsFailedError(RuntimeError):
    """Every configured provider failed with a non-transient error."""


class ExtractorNotApplicable(ValueError):
    """The provider does not handle this document (e.g. a scan for the PDF
    text-layer reader). Not a failure: it never blocks a retry."""


class FallbackDocumentExtractor:
    def __init__(self, extractors: list) -> None:
        if not extractors:
            raise ValueError("FallbackDocumentExtractor needs at least one extractor")
        self._extractors = list(extractors)
        self.provider_name = str(getattr(self._extractors[0], "provider_name", "unknown"))

    async def extract(self, document: bytes, filename: str, document_id: str) -> DocumentArtifacts:
        failures: list[tuple[str, Exception]] = []
        for index, extractor in enumerate(self._extractors):
            name = str(getattr(extractor, "provider_name", f"extractor-{index}"))
            try:
                artifacts = await extractor.extract(document, filename, document_id)
                if not artifacts.markdown.strip() and not artifacts.content_list:
                    raise ValueError(f"{name} returned no content")
            except Exception as exc:  # noqa: BLE001 - any provider failure moves to the next one
                failures.append((name, exc))
                trace_event(
                    "document_extractor",
                    "provider_skipped" if isinstance(exc, ExtractorNotApplicable) else "provider_failed",
                    document_id=document_id,
                    provider=name,
                    error_type=type(exc).__name__,
                    error=str(exc)[:300],
                    next_provider=(
                        str(getattr(self._extractors[index + 1], "provider_name", "unknown"))
                        if index + 1 < len(self._extractors)
                        else None
                    ),
                )
                continue
            if failures:
                trace_event(
                    "document_extractor",
                    "provider_fallback_used",
                    document_id=document_id,
                    provider=name,
                    failed=[failed for failed, _ in failures],
                )
            self.provider_name = name
            return artifacts

        summary = "; ".join(f"{name}: {exc}" for name, exc in failures)
        # Only when every provider that applied failed transiently is a Celery
        # retry useful.
        applied = [exc for _, exc in failures if not isinstance(exc, ExtractorNotApplicable)]
        if applied and all(isinstance(exc, TimeoutError) for exc in applied):
            raise TimeoutError(f"All document extractors timed out ({summary})") from failures[-1][1]
        raise AllExtractorsFailedError(f"All document extractors failed ({summary})") from failures[-1][1]
