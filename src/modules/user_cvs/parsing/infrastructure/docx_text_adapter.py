"""Read Word (.docx) documents locally, without an OCR service.

A .docx is a zip of XML; its text never needs OCR. Uploading it to MinerU or
PaddleOCR only adds a network round-trip that fails on a slow uplink. Uses the
standard library only. Raises ExtractorNotApplicable for anything that is not
a readable .docx so the chain falls through to OCR.
"""

from __future__ import annotations

import io
import zipfile
from xml.etree import ElementTree

from src.modules.user_cvs.parsing.domain.artifacts import DocumentArtifacts
from src.modules.user_cvs.parsing.infrastructure.fallback_extractor import ExtractorNotApplicable

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
# Same floor as the PDF reader: below it the document goes to OCR.
MIN_CHARS = 80


class NotADocxError(ExtractorNotApplicable):
    """The document is not a .docx, or it carries too little text."""


def _paragraph_text(paragraph: ElementTree.Element) -> str:
    parts: list[str] = []
    for node in paragraph.iter():
        if node.tag == f"{_W}t" and node.text:
            parts.append(node.text)
        elif node.tag in (f"{_W}tab", f"{_W}br", f"{_W}cr"):
            parts.append(" ")
    return " ".join("".join(parts).split())


class DocxTextExtractor:
    provider_name = "docx_text"

    async def extract(self, document: bytes, filename: str, document_id: str) -> DocumentArtifacts:
        del filename, document_id
        if not document.startswith(b"PK"):
            raise NotADocxError("not a .docx")
        try:
            with zipfile.ZipFile(io.BytesIO(document)) as archive:
                xml = archive.read("word/document.xml")
            root = ElementTree.fromstring(xml)
        except (KeyError, zipfile.BadZipFile, ElementTree.ParseError) as exc:
            raise NotADocxError(f"unreadable .docx: {exc}") from exc

        body = root.find(f"{_W}body")
        if body is None:
            raise NotADocxError(".docx has no body")
        # Paragraphs in document order, including those inside table cells.
        lines = [text for paragraph in body.iter(f"{_W}p") if (text := _paragraph_text(paragraph))]
        if sum(len(line.replace(" ", "")) for line in lines) < MIN_CHARS:
            raise NotADocxError("too little text in .docx")
        return DocumentArtifacts(
            markdown="\n".join(lines),
            content_list=[{"type": "text", "text": line, "page_idx": 0} for line in lines],
            extractor_version="docx-text-stdlib-1",
        )
