from src.modules.user_cvs.parsing.domain.artifacts import DocumentArtifacts
from src.workers.paddleocr import extract_document_artifacts


class PaddleOcrDocumentExtractor:
    """PaddleOCR adapter; application code depends only on document artifacts."""

    async def extract(
        self, document: bytes, filename: str, document_id: str
    ) -> DocumentArtifacts:
        return await extract_document_artifacts(document, filename, document_id)
