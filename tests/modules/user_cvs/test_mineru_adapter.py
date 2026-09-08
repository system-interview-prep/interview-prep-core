import pytest

from src.modules.user_cvs.parsing.infrastructure.mineru_adapter import MinerUDocumentExtractor


@pytest.mark.asyncio
async def test_mineru_adapter_converts_transport_failure_to_retryable_timeout(monkeypatch) -> None:
    async def fail_extract(*_args, **_kwargs):
        import httpx

        raise httpx.RemoteProtocolError("Server disconnected without sending a response.")

    monkeypatch.setattr(
        "src.modules.user_cvs.parsing.infrastructure.mineru_adapter.extract_document_artifacts",
        fail_extract,
    )

    with pytest.raises(TimeoutError, match="MinerU transport request failed"):
        await MinerUDocumentExtractor().extract(b"cv", "candidate.pdf", "cv-1")
