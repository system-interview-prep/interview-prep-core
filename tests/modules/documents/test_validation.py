from io import BytesIO
from zipfile import ZipFile

import pytest

from src.modules.documents.validation import DocumentFileValidator, InvalidDocumentFile


def _docx() -> bytes:
    output = BytesIO()
    with ZipFile(output, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", "<document/>")
    return output.getvalue()


@pytest.mark.parametrize(
    ("filename", "content_type", "content", "detected"),
    [
        ("cv.pdf", "application/pdf", b"%PDF-1.7", "application/pdf"),
        (
            "jd.docx",
            "application/octet-stream",
            _docx(),
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ),
        ("image.jpg", "image/jpeg", b"\xff\xd8\xffrest", "image/jpeg"),
    ],
)
def test_shared_validator_accepts_matching_signatures(filename, content_type, content, detected) -> None:
    assert DocumentFileValidator().validate(filename, content_type, content) == (filename, detected)


def test_shared_validator_rejects_spoofed_pdf() -> None:
    with pytest.raises(InvalidDocumentFile, match="signature"):
        DocumentFileValidator().validate("malware.pdf", "application/pdf", b"not a pdf")


def test_shared_validator_rejects_extension_mismatch() -> None:
    with pytest.raises(InvalidDocumentFile, match="extension"):
        DocumentFileValidator().validate("image.png", "image/png", b"%PDF-1.7")
