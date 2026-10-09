from types import SimpleNamespace

from src.modules.user_cvs.parsing.infrastructure import extractor_factory
from src.modules.user_cvs.parsing.infrastructure.mineru_adapter import MinerUDocumentExtractor
from src.modules.user_cvs.parsing.infrastructure.paddleocr_adapter import PaddleOCRDocumentExtractor


def test_factory_keeps_mineru_as_a_selectable_provider(monkeypatch):
    monkeypatch.setattr(extractor_factory, "get_settings", lambda: SimpleNamespace(ocr_provider="mineru"))

    assert isinstance(extractor_factory.build_document_extractor(), MinerUDocumentExtractor)


def test_factory_selects_paddleocr_from_environment_setting(monkeypatch):
    monkeypatch.setattr(extractor_factory, "get_settings", lambda: SimpleNamespace(ocr_provider="paddleocr"))

    assert isinstance(extractor_factory.build_document_extractor(), PaddleOCRDocumentExtractor)


def test_factory_accepts_existing_miuneru_typo_as_mineru_alias(monkeypatch):
    monkeypatch.setattr(extractor_factory, "get_settings", lambda: SimpleNamespace(ocr_provider="miunerU"))

    assert isinstance(extractor_factory.build_document_extractor(), MinerUDocumentExtractor)


# --- Fallback when the primary OCR provider fails -------------------------

import pytest  # noqa: E402

from src.modules.user_cvs.parsing.domain.artifacts import DocumentArtifacts  # noqa: E402
from src.modules.user_cvs.parsing.infrastructure.fallback_extractor import (  # noqa: E402
    AllExtractorsFailedError,
    FallbackDocumentExtractor,
)


def _settings(primary, fallback):
    return lambda: SimpleNamespace(ocr_provider=primary, ocr_fallback_provider=fallback)


def test_auto_fallback_pairs_mineru_with_paddleocr(monkeypatch):
    monkeypatch.setattr(extractor_factory, "get_settings", _settings("mineru", "auto"))

    extractor = extractor_factory.build_document_extractor()

    assert isinstance(extractor, FallbackDocumentExtractor)
    assert [type(e) for e in extractor._extractors] == [MinerUDocumentExtractor, PaddleOCRDocumentExtractor]


def test_fallback_none_keeps_a_single_provider(monkeypatch):
    monkeypatch.setattr(extractor_factory, "get_settings", _settings("paddleocr", "none"))

    assert isinstance(extractor_factory.build_document_extractor(), PaddleOCRDocumentExtractor)


def test_unknown_fallback_provider_is_rejected(monkeypatch):
    monkeypatch.setattr(extractor_factory, "get_settings", _settings("mineru", "tesseract"))

    with pytest.raises(ValueError, match="OCR_FALLBACK_PROVIDER"):
        extractor_factory.build_document_extractor()


class _Stub:
    def __init__(self, name, result=None, error=None):
        self.provider_name, self._result, self._error, self.calls = name, result, error, 0

    async def extract(self, document, filename, document_id):
        self.calls += 1
        if self._error:
            raise self._error
        return self._result


_OK = DocumentArtifacts(markdown="Kotlin developer", extractor_version="paddle-1")


@pytest.mark.asyncio
async def test_mineru_outage_falls_back_to_paddleocr_and_names_the_real_provider():
    mineru = _Stub("mineru", error=TimeoutError("MinerU transport request failed"))
    paddle = _Stub("paddleocr", result=_OK)
    extractor = FallbackDocumentExtractor([mineru, paddle])

    assert await extractor.extract(b"%PDF", "cv.pdf", "cv-1") is _OK
    # The pipeline reads provider_name after extract to key the stored artifact.
    assert extractor.provider_name == "paddleocr"
    assert (mineru.calls, paddle.calls) == (1, 1)


@pytest.mark.asyncio
async def test_empty_primary_output_also_falls_back():
    extractor = FallbackDocumentExtractor(
        [_Stub("mineru", result=DocumentArtifacts(markdown="  ")), _Stub("paddleocr", result=_OK)]
    )

    assert await extractor.extract(b"x", "cv.pdf", "cv-2") is _OK


@pytest.mark.asyncio
async def test_healthy_primary_is_not_followed_by_the_fallback():
    paddle = _Stub("paddleocr", result=_OK)
    extractor = FallbackDocumentExtractor([_Stub("mineru", result=_OK), paddle])

    await extractor.extract(b"x", "cv.pdf", "cv-3")

    assert extractor.provider_name == "mineru"
    assert paddle.calls == 0


@pytest.mark.asyncio
async def test_all_timeouts_stay_retryable_but_hard_failures_do_not():
    timeouts = FallbackDocumentExtractor(
        [_Stub("mineru", error=TimeoutError("t1")), _Stub("paddleocr", error=TimeoutError("t2"))]
    )
    with pytest.raises(TimeoutError, match="mineru: t1; paddleocr: t2"):
        await timeouts.extract(b"x", "cv.pdf", "cv-4")

    mixed = FallbackDocumentExtractor(
        [
            _Stub("mineru", error=TimeoutError("t1")),
            _Stub("paddleocr", error=RuntimeError("PADDLEOCR_ACCESS_TOKEN is not configured")),
        ]
    )
    with pytest.raises(AllExtractorsFailedError, match="PADDLEOCR_ACCESS_TOKEN"):
        await mixed.extract(b"x", "cv.pdf", "cv-5")


# --- Local PDF text layer before any OCR upload ---------------------------

from src.modules.user_cvs.parsing.infrastructure.pdf_text_adapter import (  # noqa: E402
    NoTextLayerError,
    PdfTextLayerExtractor,
)


def _pdf_with_text(lines: list[str]) -> bytes:
    """A minimal one-page PDF with a real text layer (no external tooling)."""
    stream = "BT /F1 12 Tf 72 720 Td 14 TL " + " ".join(f"({line}) Tj T*" for line in lines) + " ET"
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        "/Resources << /Font << /F1 5 0 R >> >> >>",
        f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = "%PDF-1.4\n", []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n{body}\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n"
    out += "".join(f"{offset:010d} 00000 n \n" for offset in offsets)
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF"
    return out.encode("latin-1")


@pytest.mark.asyncio
async def test_born_digital_pdf_is_read_locally_without_ocr():
    pdf = _pdf_with_text(["NGUYEN VAN A", "SKILLS", "Kotlin, C++, PostgreSQL, Docker and Kubernetes"] * 3)

    artifacts = await PdfTextLayerExtractor().extract(pdf, "cv.pdf", "cv-6")

    assert "Kotlin, C++, PostgreSQL" in artifacts.markdown
    assert artifacts.content_list[0] == {"type": "text", "text": "NGUYEN VAN A", "page_idx": 0}
    assert artifacts.extractor_version.startswith("pdf-text-pypdf-")


@pytest.mark.asyncio
@pytest.mark.parametrize("document", [b"\x89PNG not a pdf", _pdf_with_text(["x"])])
async def test_scans_and_non_pdfs_are_left_to_ocr(document):
    with pytest.raises(NoTextLayerError):
        await PdfTextLayerExtractor().extract(document, "cv.pdf", "cv-7")


@pytest.mark.asyncio
async def test_text_layer_skip_does_not_block_a_retry_of_timed_out_ocr():
    extractor = FallbackDocumentExtractor(
        [
            PdfTextLayerExtractor(),
            _Stub("mineru", error=TimeoutError("t1")),
            _Stub("paddleocr", error=TimeoutError("t2")),
        ]
    )
    with pytest.raises(TimeoutError):
        await extractor.extract(b"\x89PNG scan", "cv.png", "cv-8")


def test_factory_puts_the_text_layer_first(monkeypatch):
    monkeypatch.setattr(
        extractor_factory,
        "get_settings",
        lambda: SimpleNamespace(
            ocr_provider="mineru", ocr_fallback_provider="auto", pdf_text_layer_enabled=True
        ),
    )

    extractor = extractor_factory.build_document_extractor()

    assert [e.provider_name for e in extractor._extractors] == [
        "pdf_text",
        "docx_text",
        "mineru",
        "paddleocr",
    ]


# --- Word documents read locally ------------------------------------------

import io as _io  # noqa: E402
import zipfile as _zipfile  # noqa: E402

from src.modules.user_cvs.parsing.infrastructure.docx_text_adapter import (  # noqa: E402
    DocxTextExtractor,
    NotADocxError,
)


def _docx(paragraphs: list[str], table_cell: str = "") -> bytes:
    w = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    body = "".join(f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>" for text in paragraphs)
    if table_cell:
        body += f"<w:tbl><w:tr><w:tc><w:p><w:r><w:t>{table_cell}</w:t></w:r></w:p></w:tc></w:tr></w:tbl>"
    buffer = _io.BytesIO()
    with _zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", f"<w:document {w}><w:body>{body}</w:body></w:document>")
    return buffer.getvalue()


@pytest.mark.asyncio
async def test_docx_is_read_locally_including_table_cells():
    document = _docx(
        ["NGUYEN VAN A", "SKILLS", "Kotlin, C++, PostgreSQL, Docker and Kubernetes in production"],
        table_cell="Backend Developer at Acme, 2 years",
    )

    artifacts = await DocxTextExtractor().extract(document, "cv.docx", "cv-9")

    assert artifacts.markdown.splitlines()[0] == "NGUYEN VAN A"
    assert "Backend Developer at Acme, 2 years" in artifacts.markdown
    assert artifacts.extractor_version == "docx-text-stdlib-1"


@pytest.mark.asyncio
@pytest.mark.parametrize("document", [b"%PDF-1.4", _docx(["x"]), b"PK broken zip"])
async def test_non_docx_or_empty_docx_is_left_to_ocr(document):
    with pytest.raises(NotADocxError):
        await DocxTextExtractor().extract(document, "cv.docx", "cv-10")
