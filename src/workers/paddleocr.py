"""Local PaddleOCR document parsing, normalized to the application artifact contract."""

import asyncio
import importlib.metadata
import shutil
import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import Any

from src.core.config import get_settings
from src.modules.user_cvs.parsing.domain.artifacts import DocumentArtifacts

_SUPPORTED_NATIVE_INPUTS = {".pdf", ".png", ".jpg", ".jpeg", ".webp"}
_OFFICE_INPUTS = {".doc", ".docx"}


def _json_value(value: Any) -> Any:
    """Convert Paddle/Numpy values into objects safe for JSONB persistence."""
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if hasattr(value, "tolist"):
        return _json_value(value.tolist())
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _box(value: Any) -> list[float] | None:
    value = _json_value(value)
    if not isinstance(value, list) or not value:
        return None
    if len(value) == 4 and all(isinstance(item, (int, float)) for item in value):
        return [float(item) for item in value]
    points = [point for point in value if isinstance(point, list) and len(point) >= 2]
    if not points:
        return None
    try:
        xs = [float(point[0]) for point in points]
        ys = [float(point[1]) for point in points]
    except (TypeError, ValueError):
        return None
    return [min(xs), min(ys), max(xs), max(ys)]


def normalize_paddle_results(results: list[Any], *, extractor_version: str) -> DocumentArtifacts:
    """Map PP-StructureV3 page results to Markdown and page/order/bbox content blocks."""
    content_list: list[dict[str, Any]] = []
    markdown_pages: list[str] = []
    middle: list[dict[str, Any]] = []

    for page_index, result in enumerate(results):
        page = _json_value(getattr(result, "json", result))
        if not isinstance(page, dict):
            continue
        source_page_index = page.get("page_index")
        page_idx = int(source_page_index) if source_page_index is not None else page_index
        middle.append(page)
        parsed_blocks = page.get("parsing_res_list") or []
        page_markdown: list[str] = []
        if isinstance(parsed_blocks, list):
            for block in parsed_blocks:
                if not isinstance(block, dict):
                    continue
                block_text = str(block.get("block_content") or "").strip()
                if not block_text:
                    continue
                content_list.append(
                    {
                        "type": str(block.get("block_label") or "text"),
                        "text": block_text,
                        "page_idx": page_idx,
                        "bbox": _box(block.get("block_bbox")),
                        "reading_order": block.get("block_order"),
                    }
                )
                page_markdown.append(block_text)

        # Some layouts omit parsing_res_list for ordinary pages. Keep OCR line
        # results as a fallback so text is never lost when layout detection is sparse.
        if not page_markdown:
            ocr = page.get("overall_ocr_res") or {}
            texts = ocr.get("rec_texts") or []
            boxes = ocr.get("rec_boxes") or ocr.get("rec_polys") or []
            for order, text in enumerate(texts):
                rendered = str(text).strip()
                if not rendered:
                    continue
                box = _box(boxes[order]) if order < len(boxes) else None
                content_list.append(
                    {
                        "type": "text",
                        "text": rendered,
                        "page_idx": page_idx,
                        "bbox": box,
                        "reading_order": order,
                    }
                )
                page_markdown.append(rendered)
        if page_markdown:
            markdown_pages.append("\n".join(page_markdown))

    if not content_list:
        raise RuntimeError("PaddleOCR returned no document text")
    return DocumentArtifacts(
        markdown="\n\n".join(markdown_pages),
        content_list=content_list,
        middle=middle,
        extractor_version=extractor_version,
        extractor_name="paddleocr",
    )


@lru_cache(maxsize=1)
def _get_pipeline():
    try:
        from paddleocr import PPStructureV3
    except ImportError as exc:
        raise RuntimeError(
            "PaddleOCR is not installed in this worker. Install the 'ocr' project extra."
        ) from exc

    try:
        importlib.metadata.version("paddlepaddle")
    except importlib.metadata.PackageNotFoundError as exc:
        raise RuntimeError("PaddlePaddle is not installed in this worker") from exc

    return PPStructureV3(
        device=get_settings().paddleocr_device,
        use_doc_orientation_classify=True,
        use_doc_unwarping=True,
        use_textline_orientation=True,
        use_table_recognition=True,
        use_formula_recognition=False,
    )


def _office_to_pdf(source_path: Path, output_dir: Path) -> Path:
    executable = shutil.which("libreoffice") or shutil.which("soffice")
    if executable is None:
        raise RuntimeError("LibreOffice is required to convert DOC/DOCX files for PaddleOCR")
    completed = subprocess.run(
        [
            executable,
            f"-env:UserInstallation={(output_dir / 'lo-profile').as_uri()}",
            "--headless",
            "--convert-to",
            "pdf",
            "--outdir",
            str(output_dir),
            str(source_path),
        ],
        capture_output=True,
        check=False,
        timeout=90,
    )
    output_path = output_dir / f"{source_path.stem}.pdf"
    if completed.returncode != 0 or not output_path.is_file():
        message = completed.stderr.decode("utf-8", errors="replace")[-500:]
        raise RuntimeError(f"LibreOffice could not convert the document to PDF: {message}")
    return output_path


def _extract_sync(document: bytes, filename: str) -> DocumentArtifacts:
    suffix = Path(filename).suffix.casefold()
    if suffix not in _SUPPORTED_NATIVE_INPUTS | _OFFICE_INPUTS:
        raise ValueError(f"PaddleOCR does not support the '{suffix or 'unknown'}' file extension")
    version = importlib.metadata.version("paddleocr")
    with tempfile.TemporaryDirectory(prefix="paddleocr-") as temporary_directory:
        directory = Path(temporary_directory)
        source_path = directory / f"document{suffix}"
        source_path.write_bytes(document)
        prediction_path = _office_to_pdf(source_path, directory) if suffix in _OFFICE_INPUTS else source_path
        results = _get_pipeline().predict(str(prediction_path))
        return normalize_paddle_results(list(results), extractor_version=version)


async def extract_document_artifacts(
    document: bytes, filename: str, document_id: str
) -> DocumentArtifacts:
    """Run local PP-StructureV3 without blocking the worker's async event loop."""
    del document_id
    return await asyncio.to_thread(_extract_sync, document, filename)
