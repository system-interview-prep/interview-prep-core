"""Public document-ingestion contract for feature modules."""

from src.modules.documents.sse import SSE_HEADERS, status_event_stream
from src.modules.documents.validation import (
    ALLOWED_DOCUMENT_SUFFIXES,
    MAX_DOCUMENT_FILE_SIZE,
    DocumentFileTooLarge,
    DocumentFileValidator,
    InvalidDocumentFile,
    MimeDetector,
    SignatureMimeDetector,
)

__all__ = [
    "ALLOWED_DOCUMENT_SUFFIXES",
    "MAX_DOCUMENT_FILE_SIZE",
    "DocumentFileTooLarge",
    "DocumentFileValidator",
    "InvalidDocumentFile",
    "MimeDetector",
    "SignatureMimeDetector",
    "SSE_HEADERS",
    "status_event_stream",
]
