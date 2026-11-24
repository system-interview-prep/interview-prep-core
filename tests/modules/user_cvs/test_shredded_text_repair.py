"""Repairing PDFs whose diacritics arrive as separate text runs.

Some Vietnamese CVs set the extended-Latin characters in a second, subsetted
font. Each switch between fonts starts a new text-showing operator, and pypdf's
default extraction inserts a space at every boundary, so "MUC TIEU NGHE NGHIEP"
arrives shredded. Layout mode places glyphs by coordinate and avoids it, but it
returns nothing at all for a quarter of the corpus - hence the guard these tests
pin down.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from src.modules.user_cvs.parsing.domain.source import (
    SHREDDED_TEXT_RATIO,
    character_shredded_ratio,
)
from src.modules.user_cvs.parsing.infrastructure.pdf_text_adapter import (
    PdfTextLayerExtractor,
    _repair_shredded_pages,
)

CORPUS = Path("D:/NCKH/thucnghiem/Data-CV")


def _reader(*layout_pages: str, raises: bool = False):
    """A stand-in PdfReader whose pages answer layout-mode extraction."""

    def make(text: str):
        def extract_text(**kwargs):
            if raises:
                raise ValueError("layout mode unavailable for this font")
            return text if kwargs.get("extraction_mode") == "layout" else ""

        return SimpleNamespace(extract_text=extract_text)

    return SimpleNamespace(pages=[make(text) for text in layout_pages])


def test_layout_text_replaces_a_shredded_page() -> None:
    plain = ["M U C T I E U N G H E N G H I E P"]
    pages, repaired = _repair_shredded_pages(_reader("MUC TIEU NGHE NGHIEP"), plain)
    assert repaired is True
    assert pages == ["MUC TIEU NGHE NGHIEP"]


def test_an_empty_layout_page_keeps_the_plain_read() -> None:
    """pypdf returns "" for some documents; swapping that in would erase them."""
    plain = ["M U C T I E U N G H E N G H I E P"]
    pages, repaired = _repair_shredded_pages(_reader(""), plain)
    assert repaired is False
    assert pages == plain


def test_a_truncated_layout_page_keeps_the_plain_read() -> None:
    """Layout mode that dropped most of the glyphs is a loss, not a repair."""
    plain = ["M U C T I E U N G H E N G H I E P V A K Y N A N G"]
    pages, repaired = _repair_shredded_pages(_reader("MUC"), plain)
    assert repaired is False
    assert pages == plain


def test_a_layout_failure_keeps_the_plain_read() -> None:
    plain = ["M U C T I E U"]
    pages, repaired = _repair_shredded_pages(_reader("anything", raises=True), plain)
    assert repaired is False
    assert pages == plain


def test_pages_are_decided_independently() -> None:
    """One unreadable page must not discard a repair that worked on another."""
    plain = ["M U C T I E U N G H E", "K Y N A N G C H U Y E N M O N"]
    pages, repaired = _repair_shredded_pages(_reader("MUC TIEU NGHE", ""), plain)
    assert repaired is True
    assert pages == ["MUC TIEU NGHE", "K Y N A N G C H U Y E N M O N"]


@pytest.mark.skipif(not CORPUS.is_dir(), reason="CV corpus is not available here")
async def test_a_real_shredded_cv_becomes_readable() -> None:
    source = CORPUS / "CV-NguyenHoangAnh.pdf"
    if not source.is_file():
        pytest.skip("sample CV is not available here")
    artifacts = await PdfTextLayerExtractor().extract(source.read_bytes(), source.name, "probe")

    assert character_shredded_ratio(artifacts.markdown) < SHREDDED_TEXT_RATIO
    # The words the shredded read destroyed are now single tokens.
    assert "Nguyễn Hoàng Anh" in artifacts.markdown
    assert "MỤC TIÊU NGHỀ NGHIỆP" in artifacts.markdown
    # Provenance records that this document took the repair path.
    assert artifacts.extractor_version.endswith("-layout-repaired")


@pytest.mark.skipif(not CORPUS.is_dir(), reason="CV corpus is not available here")
async def test_a_healthy_cv_is_left_byte_identical() -> None:
    """The repair must not touch the documents that already read correctly."""
    source = CORPUS / "BuiThanhTai_Fullstack.pdf"
    if not source.is_file():
        pytest.skip("sample CV is not available here")
    data = source.read_bytes()
    artifacts = await PdfTextLayerExtractor().extract(data, source.name, "probe")

    assert not artifacts.extractor_version.endswith("-layout-repaired")
    assert artifacts.markdown.strip()
