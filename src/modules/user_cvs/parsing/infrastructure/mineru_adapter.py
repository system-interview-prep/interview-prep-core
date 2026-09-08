import httpx

from src.modules.user_cvs.parsing.domain.artifacts import DocumentArtifacts
from src.workers.mineru import extract_document_artifacts


class MinerUDocumentExtractor:
    """MinerU adapter; application code does not depend on MinerU APIs."""

    async def extract(
        self, document: bytes, filename: str, document_id: str
    ) -> DocumentArtifacts:
        try:
            return await extract_document_artifacts(document, filename, document_id)
        except httpx.TransportError as exc:
            # The Celery task retries TimeoutError with exponential backoff.  A
            # disconnected presigned-upload endpoint is transient in the same
            # way as a socket timeout, so expose one application-level signal
            # instead of leaking httpx exception classes into task policy.
            raise TimeoutError("MinerU transport request failed") from exc
