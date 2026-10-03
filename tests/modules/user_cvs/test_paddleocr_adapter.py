import httpx
import pytest

from src.modules.user_cvs.parsing.infrastructure.paddleocr_adapter import PaddleOCRDocumentExtractor


@pytest.mark.asyncio
async def test_paddleocr_adapter_converts_transport_failure_to_retryable_timeout(monkeypatch) -> None:
    async def fail_extract(*_args, **_kwargs):
        raise httpx.RemoteProtocolError("server disconnected")

    monkeypatch.setattr(
        "src.modules.user_cvs.parsing.infrastructure.paddleocr_adapter.extract_document_artifacts",
        fail_extract,
    )

    with pytest.raises(TimeoutError, match="PaddleOCR transport request failed"):
        await PaddleOCRDocumentExtractor().extract(b"cv", "candidate.pdf", "cv-1")
