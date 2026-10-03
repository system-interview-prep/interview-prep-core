from src.modules.user_cvs.parsing.domain.artifacts import DocumentArtifacts
from src.modules.user_cvs.parsing.infrastructure.paddleocr_adapter import PaddleOcrDocumentExtractor


async def test_paddleocr_adapter_returns_provider_neutral_artifacts(monkeypatch) -> None:
    artifacts = DocumentArtifacts(markdown="Skills\nPython", extractor_name="paddleocr")

    async def extract(*_args):
        return artifacts

    monkeypatch.setattr(
        "src.modules.user_cvs.parsing.infrastructure.paddleocr_adapter.extract_document_artifacts",
        extract,
    )

    assert await PaddleOcrDocumentExtractor().extract(b"cv", "candidate.pdf", "cv-1") == artifacts
