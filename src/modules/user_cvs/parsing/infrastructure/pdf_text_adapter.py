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
from src.modules.user_cvs.parsing.domain.source import (
    SHREDDED_TEXT_RATIO,
    character_shredded_ratio,
)
from src.modules.user_cvs.parsing.infrastructure.fallback_extractor import ExtractorNotApplicable

# Below this the text layer is treated as missing (scanned page, a few stray
# glyphs) and the document goes to OCR.
MIN_CHARS_PER_PAGE = 80

# A page's layout-mode text is accepted only if it kept this share of the
# plain-mode alphanumerics. pypdf's layout mode returns nothing at all for some
# documents (57 of the 230-CV corpus), so an unguarded swap would silently empty
# a quarter of them.
_LAYOUT_MIN_ALNUM_SHARE = 0.60


def _repair_shredded_pages(reader, plain: list[str]) -> tuple[list[str], bool]:
    """Re-read a glyph-shredded PDF using character positions instead of runs.

    Some PDFs set Vietnamese diacritic characters in a second, subsetted font:
    every switch between the Latin-1 font and the extended one starts a new
    text-showing operator, and pypdf's default extraction puts a space at each
    boundary. "MUC TIEU NGHE NGHIEP" then arrives as "MU C TIEU NGH E NGHI E P",
    and no skill, degree or employer survives as a matchable token. Layout mode
    places glyphs by coordinate, so those boundaries produce no space.

    Only a page that the default read shredded is replaced, and only when layout
    mode actually returned comparable text for it, so documents that already
    read correctly keep byte-identical text and their evidence offsets.
    """
    try:
        layout = [page.extract_text(extraction_mode="layout") or "" for page in reader.pages]
    except Exception:
        # Layout mode is best-effort: a failure leaves the plain read in place,
        # still flagged downstream as a damaged read.
        return plain, False
    repaired: list[str] = []
    used = False
    for index, page_text in enumerate(plain):
        candidate = layout[index] if index < len(layout) else ""
        plain_alnum = sum(character.isalnum() for character in page_text)
        candidate_alnum = sum(character.isalnum() for character in candidate)
        if candidate_alnum > 0 and candidate_alnum >= _LAYOUT_MIN_ALNUM_SHARE * plain_alnum:
            repaired.append(candidate)
            used = True
        else:
            repaired.append(page_text)
    return repaired, used


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
        repaired = False
        if character_shredded_ratio("\n".join(pages)) >= SHREDDED_TEXT_RATIO:
            pages, repaired = _repair_shredded_pages(reader, pages)
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
            # The suffix appears only when a repair actually fired, so documents
            # that never needed one keep their existing version string.
            extractor_version=(
                f"pdf-text-pypdf-{__version__}-layout-repaired"
                if repaired
                else f"pdf-text-pypdf-{__version__}"
            ),
        )
