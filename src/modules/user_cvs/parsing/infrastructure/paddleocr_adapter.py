"""PaddleOCR adapter implementing the document extraction port."""

import httpx

from src.modules.user_cvs.parsing.domain.artifacts import DocumentArtifacts
from src.workers.paddleocr import extract_document_artifacts


class PaddleOCRDocumentExtractor:
    """PaddleOCR Online adapter; parsers remain unaware of provider details."""

    provider_name = "paddleocr"

    async def extract(
        self, document: bytes, filename: str, document_id: str
    ) -> DocumentArtifacts:
        try:
            return await extract_document_artifacts(document, filename, document_id)
        except httpx.TransportError as exc:
            raise TimeoutError("PaddleOCR transport request failed") from exc
