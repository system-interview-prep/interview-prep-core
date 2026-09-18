"""Public document-ingestion contract for feature modules."""

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
]
