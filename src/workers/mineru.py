import asyncio
import io
import json
from pathlib import Path
from time import monotonic
from typing import Any
from zipfile import BadZipFile, ZipFile

import httpx

from src.core.config import get_settings
from src.modules.user_cvs.parsing.domain.artifacts import DocumentArtifacts

_MAX_ARCHIVE_UNCOMPRESSED_BYTES = 100 * 1024 * 1024


def _error(response: httpx.Response, context: str) -> str:
    try:
        body = response.json()
    except ValueError:
        return f"MinerU {context} failed (HTTP {response.status_code})"
    message = body.get("msg") or body.get("message") or "unknown API error"
    return f"MinerU {context} failed: {message}"


def _find_member(names: list[str], suffixes: tuple[str, ...]) -> str | None:
    return next((name for name in names if name.lower().endswith(suffixes)), None)


def _read_json_member(archive: ZipFile, name: str | None) -> Any:
    if name is None:
        return None
    try:
        return json.loads(archive.read(name).decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"MinerU result archive contains invalid JSON: {name}") from exc


def read_document_artifacts(archive_bytes: bytes) -> DocumentArtifacts:
    """Read MinerU output without extracting untrusted archive paths to disk."""
    try:
        with ZipFile(io.BytesIO(archive_bytes)) as archive:
            if sum(item.file_size for item in archive.infolist()) > _MAX_ARCHIVE_UNCOMPRESSED_BYTES:
                raise RuntimeError("MinerU result archive exceeds the uncompressed size limit")
            names = archive.namelist()
            markdown_name = _find_member(names, ("/full.md", "full.md"))
            markdown_name = markdown_name or _find_member(names, (".md",))
            if not markdown_name:
                raise RuntimeError("MinerU result archive does not contain Markdown")
            markdown = archive.read(markdown_name).decode("utf-8-sig")

            content_name = _find_member(names, ("_content_list_v2.json", "content_list_v2.json"))
            content_name = content_name or _find_member(
                names, ("_content_list.json", "content_list.json")
            )
            middle_name = _find_member(names, ("_middle.json", "middle.json"))
            content_list = _read_json_member(archive, content_name) or []
            middle = _read_json_member(archive, middle_name)
            if not isinstance(content_list, list):
                raise RuntimeError("MinerU content list must be a JSON array")
            if middle is not None and not isinstance(middle, (dict, list)):
                raise RuntimeError("MinerU middle output must be a JSON object or array")
            extractor_version = middle.get("_version_name") if isinstance(middle, dict) else None
            return DocumentArtifacts(
                markdown=markdown,
                content_list=content_list,
                middle=middle,
                extractor_version=str(extractor_version) if extractor_version else None,
            )
    except BadZipFile as exc:
        raise RuntimeError("MinerU returned an invalid result archive") from exc


async def extract_document_artifacts(
    document: Path | bytes, filename: str, data_id: str
) -> DocumentArtifacts:
    """Parse a locally stored document through MinerU Precision Extract API."""
    settings = get_settings()
    if not settings.mineru_api_key:
        raise RuntimeError("MINERU_API_KEY is not configured")

    request_body = {
        "files": [{"name": filename, "data_id": data_id}],
        "model_version": settings.mineru_model_version,
        "language": settings.mineru_language,
        "is_ocr": True,
        "enable_table": True,
        "enable_formula": False,
    }
    base_url = settings.mineru_base_url.rstrip("/")
    headers = {"Authorization": f"Bearer {settings.mineru_api_key}"}
    timeout = httpx.Timeout(
        connect=settings.mineru_http_connect_timeout_seconds,
        read=settings.mineru_http_read_timeout_seconds,
        write=settings.mineru_http_write_timeout_seconds,
        pool=settings.mineru_http_pool_timeout_seconds,
    )
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        response = await client.post(f"{base_url}/file-urls/batch", headers=headers, json=request_body)
        if response.status_code != 200:
            raise RuntimeError(_error(response, "upload URL request"))
        result = response.json()
        if result.get("code") != 0:
            raise RuntimeError(f"MinerU upload URL request failed: {result.get('msg', 'unknown API error')}")
        data = result.get("data") or {}
        upload_urls = data.get("file_urls") or []
        batch_id = data.get("batch_id")
        if not batch_id or len(upload_urls) != 1:
            raise RuntimeError("MinerU did not return an upload URL")

        content = document if isinstance(document, bytes) else document.read_bytes()
        upload_response = await client.put(upload_urls[0], content=content)
        if upload_response.status_code not in (200, 201):
            raise RuntimeError(_error(upload_response, "file upload"))

        deadline = monotonic() + settings.mineru_timeout_seconds
        while monotonic() < deadline:
            await asyncio.sleep(settings.mineru_poll_interval_seconds)
            response = await client.get(f"{base_url}/extract-results/batch/{batch_id}", headers=headers)
            if response.status_code != 200:
                raise RuntimeError(_error(response, "result query"))
            result = response.json()
            if result.get("code") != 0:
                raise RuntimeError(f"MinerU result query failed: {result.get('msg', 'unknown API error')}")
            extract_results = (result.get("data") or {}).get("extract_result") or []
            task_result = next((item for item in extract_results if item.get("data_id") == data_id), None)
            if task_result is None and len(extract_results) == 1:
                task_result = extract_results[0]
            if not task_result:
                continue
            if task_result.get("state") == "failed":
                error = task_result.get("err_msg") or "unknown error"
                raise RuntimeError(f"MinerU extraction failed: {error}")
            if task_result.get("state") != "done":
                continue
            zip_url = task_result.get("full_zip_url")
            if not zip_url:
                raise RuntimeError("MinerU completed without a result archive")
            archive_response = await client.get(zip_url)
            archive_response.raise_for_status()
            return read_document_artifacts(archive_response.content)
    raise TimeoutError(f"MinerU extraction timed out after {settings.mineru_timeout_seconds} seconds")


async def extract_markdown(document: Path | bytes, filename: str, data_id: str) -> str:
    """Backward-compatible Markdown-only wrapper."""
    artifacts = await extract_document_artifacts(document, filename, data_id)
    return artifacts.markdown
