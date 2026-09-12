"""Content-based validation shared by CV and job-description uploads."""

from io import BytesIO
from pathlib import Path
from typing import Protocol
from zipfile import BadZipFile, ZipFile

MAX_DOCUMENT_FILE_SIZE = 10 * 1024 * 1024
ALLOWED_DOCUMENT_SUFFIXES = frozenset({".pdf", ".doc", ".docx", ".png", ".jpg", ".jpeg", ".webp"})
MIME_BY_SUFFIX = {
    ".pdf": "application/pdf",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}


class InvalidDocumentFile(ValueError):
    pass


class DocumentFileTooLarge(InvalidDocumentFile):
    pass


class MimeDetector(Protocol):
    def detect(self, content: bytes) -> str: ...


class SignatureMimeDetector:
    """Identify supported formats from bytes instead of trusting client metadata."""

    def detect(self, content: bytes) -> str:
        if content.startswith(b"%PDF-"):
            return "application/pdf"
        if content.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if content.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if content.startswith(b"RIFF") and content[8:12] == b"WEBP":
            return "image/webp"
        if content.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
            return "application/msword"
        if content.startswith(b"PK"):
            try:
                with ZipFile(BytesIO(content)) as archive:
                    names = set(archive.namelist())
                    if "[Content_Types].xml" in names and "word/document.xml" in names:
                        return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            except BadZipFile:
                pass
        raise InvalidDocumentFile("File signature is not a supported document type")


class DocumentFileValidator:
    def __init__(
        self,
        mime_detector: MimeDetector | None = None,
        *,
        max_size: int = MAX_DOCUMENT_FILE_SIZE,
    ) -> None:
        self._mime_detector = mime_detector or SignatureMimeDetector()
        self._max_size = max_size

    def validate(self, filename: str, content_type: str | None, content: bytes) -> tuple[str, str]:
        safe_name = Path(filename).name
        suffix = Path(safe_name).suffix.lower()
        if not safe_name or suffix not in ALLOWED_DOCUMENT_SUFFIXES:
            raise InvalidDocumentFile("Only PDF, DOC, DOCX, PNG, JPEG, and WEBP files are allowed")
        if not content:
            raise InvalidDocumentFile("File is empty")
        if len(content) > self._max_size:
            raise DocumentFileTooLarge("File must not exceed 10 MB")
        detected_content_type = self._mime_detector.detect(content)
        if detected_content_type != MIME_BY_SUFFIX[suffix]:
            raise InvalidDocumentFile("File content does not match its extension")
        if content_type and content_type not in {detected_content_type, "application/octet-stream"}:
            raise InvalidDocumentFile("File content does not match its declared content type")
        return safe_name, detected_content_type
