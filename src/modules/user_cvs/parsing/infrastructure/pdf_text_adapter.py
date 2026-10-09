"""Read the text layer of born-digital PDFs locally, without an OCR service.

Most CVs and JDs are exported from Word/Canva/LaTeX and already carry their
text. Sending them to MinerU or PaddleOCR means uploading the whole file (a
1-page CV with embedded fonts is ~4 MB), which fails on a slow uplink with
transport errors although no OCR is needed. This extractor answers locally
when the PDF has a usable text layer and otherwise raises, so the fallback
chain moves on to the OCR providers (scans, image-only PDFs, other formats).
"""

from __future__ import annotations

import io
import re

from src.modules.user_cvs.parsing.domain.artifacts import DocumentArtifacts
from src.modules.user_cvs.parsing.infrastructure.fallback_extractor import ExtractorNotApplicable

# Below this the text layer is treated as missing (scanned page, a few stray
# glyphs) and the document goes to OCR.
MIN_CHARS_PER_PAGE = 80


class NoTextLayerError(ExtractorNotApplicable):
    """The document is not a PDF or its text layer is too thin to trust."""


class PdfTextLayerExtractor:
    provider_name = "pdf_text"

    async def extract(self, document: bytes, filename: str, document_id: str) -> DocumentArtifacts:
        del filename, document_id
        if not document.lstrip()[:5].startswith(b"%PDF"):
            raise NoTextLayerError("not a PDF")
        try:
            from pypdf import PdfReader, __version__
        except ImportError as exc:  # optional at runtime: fall through to OCR
            raise NoTextLayerError("pypdf is not installed") from exc

        try:
            reader = PdfReader(io.BytesIO(document))
            pages = [page.extract_text() or "" for page in reader.pages]
        except Exception as exc:  # encrypted or malformed: let OCR try
            raise NoTextLayerError(f"unreadable PDF: {exc}") from exc
        if not pages:
            raise NoTextLayerError("PDF has no pages")
        visible = sum(len(re.sub(r"\s+", "", text)) for text in pages)
        if visible < MIN_CHARS_PER_PAGE * len(pages):
            raise NoTextLayerError(f"text layer too thin ({visible} chars on {len(pages)} pages)")

        content_list: list[dict] = []
        markdown_pages: list[str] = []
        for page_idx, text in enumerate(pages):
            lines = [" ".join(line.split()) for line in text.splitlines()]
            lines = [line for line in lines if line]
            content_list.extend({"type": "text", "text": line, "page_idx": page_idx} for line in lines)
            markdown_pages.append("\n".join(lines))
        return DocumentArtifacts(
            markdown="\n\n".join(markdown_pages),
            content_list=content_list,
            extractor_version=f"pdf-text-pypdf-{__version__}",
        )
