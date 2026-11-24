"""PaddleOCR Official API client used by the PaddleOCR document adapter.

The hosted API is asynchronous: upload a document, poll a job, then download
the JSONL result.  This module deliberately returns the same provider-neutral
``DocumentArtifacts`` contract as the MinerU worker.
"""

import asyncio
import json
from pathlib import Path
from time import monotonic
from typing import Any

import httpx

from src.core.config import get_settings
from src.modules.user_cvs.parsing.domain.artifacts import DocumentArtifacts

_API_PATH = "/api/v2/ocr/jobs"
_DONE_STATES = {"done", "completed", "success", "succeeded"}
_FAILED_STATES = {"failed", "failure", "error"}


def _payload(response: httpx.Response, context: str) -> dict[str, Any]:
    try:
        value = response.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"PaddleOCR {context} returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"PaddleOCR {context} returned an invalid response")
    return value


def _check_response(response: httpx.Response, context: str) -> dict[str, Any]:
    body = _payload(response, context)
    if response.status_code < 200 or response.status_code >= 300:
        message = body.get("msg") or body.get("message") or f"HTTP {response.status_code}"
        raise RuntimeError(f"PaddleOCR {context} failed: {message}")
    if body.get("code") not in (None, 0):
        raise RuntimeError(f"PaddleOCR {context} failed: {body.get('msg') or 'unknown API error'}")
    return body


def _job_id(body: dict[str, Any]) -> str:
    data = body.get("data") or {}
    value = data.get("jobId") or data.get("job_id") or body.get("jobId")
    if not value:
        raise RuntimeError("PaddleOCR did not return a job ID")
    return str(value)


def _status_data(body: dict[str, Any]) -> dict[str, Any]:
    data = body.get("data") or body
    return data if isinstance(data, dict) else {}


def _state(data: dict[str, Any]) -> str:
    return str(data.get("state") or data.get("status") or "").casefold()


def _result_url(data: dict[str, Any]) -> str | None:
    value = data.get("resultUrl") or data.get("result_url")
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return value.get("jsonUrl") or value.get("json_url") or value.get("url")
    return data.get("jsonUrl") or data.get("json_url")


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("text", "markdownText", "markdown_text", "content"):
            if isinstance(value.get(key), str):
                return value[key]
    return ""


def _box(value: Any) -> list[float] | None:
    if isinstance(value, list) and len(value) == 4 and all(isinstance(item, (int, float)) for item in value):
        return [float(item) for item in value]
    if isinstance(value, list) and value and all(isinstance(item, list) for item in value):
        points = [
            point
            for point in value
            if len(point) >= 2 and all(isinstance(v, (int, float)) for v in point)
        ]
        if points:
            xs = [point[0] for point in points]
            ys = [point[1] for point in points]
            return [float(min(xs)), float(min(ys)), float(max(xs)), float(max(ys))]
    return None


def _ocr_page_items(page: dict[str, Any], page_index: int) -> list[dict[str, Any]]:
    result = page.get("prunedResult") or page.get("pruned_result") or page
    if not isinstance(result, dict):
        return []
    texts = result.get("rec_texts") or result.get("recTexts") or result.get("texts") or []
    boxes = result.get("rec_boxes") or result.get("recBoxes") or result.get("dt_polys") or []
    if not isinstance(texts, list):
        return []
    items: list[dict[str, Any]] = []
    for index, value in enumerate(texts):
        if not isinstance(value, str) or not value.strip():
            continue
        item: dict[str, Any] = {"type": "text", "text": value, "page_idx": page_index}
        if isinstance(boxes, list) and index < len(boxes):
            bbox = _box(boxes[index])
            if bbox:
                item["bbox"] = bbox
        items.append(item)
    return items


def _normalize_results(result_lines: list[dict[str, Any]], job_id: str, model: str) -> DocumentArtifacts:
    markdown_parts: list[str] = []
    content_list: list[Any] = []

    for line in result_lines:
        result = line.get("result", line)
        if not isinstance(result, dict):
            continue
        layout_pages = result.get("layoutParsingResults") or result.get("layout_parsing_results") or []
        if isinstance(layout_pages, list):
            for page_index, page in enumerate(layout_pages):
                if not isinstance(page, dict):
                    continue
                markdown = _text(page.get("markdown") or page.get("markdownText"))
                if markdown.strip():
                    markdown_parts.append(markdown)
                    content_list.append(
                        {"type": "markdown", "text": markdown, "page_idx": page_index}
                    )

        ocr_pages = result.get("ocrResults") or result.get("ocr_results") or []
        if isinstance(ocr_pages, list):
            for page_index, page in enumerate(ocr_pages):
                if not isinstance(page, dict):
                    continue
                items = _ocr_page_items(page, page_index)
                if items:
                    content_list.extend(items)
                    markdown_parts.append("\n".join(item["text"] for item in items))

        if not layout_pages and not ocr_pages:
            markdown = _text(result.get("markdown") or result.get("markdownText"))
            if markdown.strip():
                markdown_parts.append(markdown)

    markdown = "\n\n".join(part for part in markdown_parts if part.strip()).strip()
    if not markdown and not content_list:
        raise RuntimeError("PaddleOCR completed without readable text")
    return DocumentArtifacts(
        markdown=markdown,
        content_list=content_list,
        middle={"jobId": job_id, "model": model, "resultLines": result_lines},
        extractor_version=f"paddleocr-{model}",
    )


async def extract_document_artifacts(
    document: Path | bytes, filename: str, data_id: str
) -> DocumentArtifacts:
    """Extract a document through PaddleOCR's hosted AI Studio API."""
    settings = get_settings()
    if not settings.paddleocr_access_token:
        raise RuntimeError("PADDLEOCR_ACCESS_TOKEN is not configured")

    content = document if isinstance(document, bytes) else document.read_bytes()
    base_url = settings.paddleocr_base_url.rstrip("/")
    headers = {"Authorization": f"Bearer {settings.paddleocr_access_token}"}
    timeout = httpx.Timeout(settings.paddleocr_request_timeout_seconds)
    optional_payload = {
        "useDocOrientationClassify": False,
        "useDocUnwarping": False,
    }
    if settings.paddleocr_model.casefold().startswith("pp-ocr"):
        optional_payload["useTextlineOrientation"] = False
    else:
        optional_payload["useChartRecognition"] = False
    data = {
        "model": settings.paddleocr_model,
        "optionalPayload": json.dumps(optional_payload),
    }
    files = {"file": (filename, content, "application/octet-stream")}

    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        response = await client.post(f"{base_url}{_API_PATH}", headers=headers, data=data, files=files)
        job_id = _job_id(_check_response(response, "job submission"))
        deadline = monotonic() + settings.paddleocr_timeout_seconds
        result_url: str | None = None
        while monotonic() < deadline:
            response = await client.get(f"{base_url}{_API_PATH}/{job_id}", headers=headers)
            status_body = _check_response(response, "job status query")
            status_data = _status_data(status_body)
            state = _state(status_data)
            if state in _FAILED_STATES:
                message = status_data.get("errorMsg") or status_data.get("error_msg") or "unknown error"
                raise RuntimeError(f"PaddleOCR extraction failed: {message}")
            if state in _DONE_STATES:
                result_url = _result_url(status_data)
                break
            await asyncio.sleep(settings.paddleocr_poll_interval_seconds)

        if not result_url:
            raise TimeoutError(
                f"PaddleOCR extraction timed out after {settings.paddleocr_timeout_seconds} seconds"
            )
        result_response = await client.get(result_url)
        if result_response.status_code < 200 or result_response.status_code >= 300:
            raise RuntimeError(f"PaddleOCR result download failed (HTTP {result_response.status_code})")
        lines: list[dict[str, Any]] = []
        for raw_line in result_response.text.splitlines():
            if raw_line.strip():
                value = json.loads(raw_line)
                if isinstance(value, dict):
                    lines.append(value)
        if not lines:
            raise RuntimeError("PaddleOCR result download was empty")
        return _normalize_results(lines, job_id, settings.paddleocr_model)
