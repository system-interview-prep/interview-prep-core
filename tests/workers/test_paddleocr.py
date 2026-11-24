import json
from types import SimpleNamespace

import pytest

from src.workers import paddleocr


class FakeResponse:
    def __init__(self, payload=None, *, text="", status_code=200):
        self._payload = payload
        self.text = text
        self.status_code = status_code

    def json(self):
        return self._payload


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

    async def get(self, url, **kwargs):
        self.requests.append(("GET", url, kwargs))
        return next(self.responses)


def _settings(token="test-token"):
    return SimpleNamespace(
        paddleocr_access_token=token,
        paddleocr_base_url="https://paddleocr.example",
        paddleocr_model="PaddleOCR-VL-1.6",
        paddleocr_request_timeout_seconds=20,
        paddleocr_poll_interval_seconds=0,
        paddleocr_timeout_seconds=10,
    )


@pytest.mark.asyncio
async def test_extract_document_artifacts_uploads_polls_and_normalizes_markdown(monkeypatch):
    result = {
        "result": {
            "layoutParsingResults": [
                {"markdown": {"text": "# Skills\n\nPython"}},
                {"markdownText": "English: B2"},
            ]
        }
    }
    client = FakeClient(
        [
            FakeResponse({"code": 0, "data": {"jobId": "job-1"}}),
            FakeResponse({"code": 0, "data": {"state": "done", "resultUrl": {"jsonUrl": "https://result"}}}),
            FakeResponse(text=json.dumps(result)),
        ]
    )
    monkeypatch.setattr(paddleocr, "get_settings", lambda: _settings())
    monkeypatch.setattr(paddleocr.httpx, "AsyncClient", lambda **_: client)

    artifacts = await paddleocr.extract_document_artifacts(b"pdf", "resume.pdf", "cv-1")

    assert artifacts.extractor_version == "paddleocr-PaddleOCR-VL-1.6"
    assert artifacts.markdown == "# Skills\n\nPython\n\nEnglish: B2"
    assert artifacts.content_list[0]["page_idx"] == 0
    assert [request[0] for request in client.requests] == ["POST", "GET", "GET"]
    request = client.requests[0][2]
    assert request["data"]["model"] == "PaddleOCR-VL-1.6"
    assert json.loads(request["data"]["optionalPayload"])["useDocOrientationClassify"] is False
    assert request["files"]["file"][0] == "resume.pdf"


@pytest.mark.asyncio
async def test_extract_document_artifacts_normalizes_pure_ocr_result(monkeypatch):
    result = {
        "result": {
            "ocrResults": [
                {
                    "prunedResult": {
                        "rec_texts": ["Skills", "Python"],
                        "rec_boxes": [[1, 2, 3, 4], [5, 6, 7, 8]],
                    }
                }
            ]
        }
    }
    client = FakeClient(
        [
            FakeResponse({"code": 0, "data": {"jobId": "job-2"}}),
            FakeResponse({"code": 0, "data": {"status": "completed", "resultUrl": "https://result"}}),
            FakeResponse(text=json.dumps(result)),
        ]
    )
    monkeypatch.setattr(paddleocr, "get_settings", lambda: _settings())
    monkeypatch.setattr(paddleocr.httpx, "AsyncClient", lambda **_: client)

    artifacts = await paddleocr.extract_document_artifacts(b"image", "resume.png", "cv-2")

    assert artifacts.markdown == "Skills\nPython"
    assert artifacts.content_list[1]["bbox"] == [5.0, 6.0, 7.0, 8.0]


@pytest.mark.asyncio
async def test_extract_document_artifacts_requires_access_token(monkeypatch):
    monkeypatch.setattr(paddleocr, "get_settings", lambda: _settings(token=None))

    with pytest.raises(RuntimeError, match="PADDLEOCR_ACCESS_TOKEN is not configured"):
        await paddleocr.extract_document_artifacts(b"pdf", "resume.pdf", "cv-3")
