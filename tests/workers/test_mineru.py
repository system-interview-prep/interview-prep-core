import io
from types import SimpleNamespace
from zipfile import ZipFile

import pytest

from src.workers import mineru


class FakeResponse:
    def __init__(self, payload=None, status_code=200, content=b""):
        self._payload = payload
        self.status_code = status_code
        self.content = content

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeClient:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def post(self, url, **kwargs):
        self.requests.append(("POST", url, kwargs))
        return next(self.responses)

    async def put(self, url, **kwargs):
        self.requests.append(("PUT", url, kwargs))
        return next(self.responses)

    async def get(self, url, **kwargs):
        self.requests.append(("GET", url, kwargs))
        return next(self.responses)


def _settings(api_key="test-key"):
    return SimpleNamespace(
        mineru_api_key=api_key,
        mineru_base_url="https://mineru.example/api/v4",
        mineru_model_version="vlm",
        mineru_language="auto",
        mineru_poll_interval_seconds=0,
        mineru_timeout_seconds=10,
    )


def _archive(markdown="# Parsed JD\nPython"):
    buffer = io.BytesIO()
    with ZipFile(buffer, "w") as archive:
        archive.writestr("result/full.md", markdown)
    return buffer.getvalue()


@pytest.mark.asyncio
async def test_extract_markdown_uploads_polls_and_reads_full_markdown(monkeypatch, tmp_path):
    document = tmp_path / "jd.pdf"
    document.write_bytes(b"document")
    client = FakeClient(
        [
            FakeResponse({"code": 0, "data": {"batch_id": "batch-1", "file_urls": ["https://upload"]}}),
            FakeResponse(),
            FakeResponse(
                {
                    "code": 0,
                    "data": {
                        "extract_result": [
                            {"data_id": "jd-1", "state": "done", "full_zip_url": "https://result"}
                        ]
                    },
                }
            ),
            FakeResponse(content=_archive()),
        ]
    )
    monkeypatch.setattr(mineru, "get_settings", _settings)
    monkeypatch.setattr(mineru.httpx, "AsyncClient", lambda **_: client)

    markdown = await mineru.extract_markdown(document, "jd.pdf", "jd-1")

    assert markdown == "# Parsed JD\nPython"
    assert [request[0] for request in client.requests] == ["POST", "PUT", "GET", "GET"]
    request_body = client.requests[0][2]["json"]
    assert request_body["files"] == [{"name": "jd.pdf", "data_id": "jd-1"}]
    assert request_body["is_ocr"] is True


@pytest.mark.asyncio
async def test_extract_markdown_reports_mineru_task_failure(monkeypatch, tmp_path):
    document = tmp_path / "cv.pdf"
    document.write_bytes(b"document")
    client = FakeClient(
        [
            FakeResponse({"code": 0, "data": {"batch_id": "batch-1", "file_urls": ["https://upload"]}}),
            FakeResponse(),
            FakeResponse(
                {
                    "code": 0,
                    "data": {"extract_result": [{"state": "failed", "err_msg": "unsupported file"}]},
                }
            ),
        ]
    )
    monkeypatch.setattr(mineru, "get_settings", _settings)
    monkeypatch.setattr(mineru.httpx, "AsyncClient", lambda **_: client)

    with pytest.raises(RuntimeError, match="MinerU extraction failed: unsupported file"):
        await mineru.extract_markdown(document, "cv.pdf", "cv-1")


@pytest.mark.asyncio
async def test_extract_markdown_requires_api_key(monkeypatch, tmp_path):
    document = tmp_path / "cv.pdf"
    document.write_bytes(b"document")
    monkeypatch.setattr(mineru, "get_settings", lambda: _settings(api_key=None))

    with pytest.raises(RuntimeError, match="MINERU_API_KEY is not configured"):
        await mineru.extract_markdown(document, "cv.pdf", "cv-1")
