"""Public document-source contract reused by evidence-grounded consumers."""

from src.modules.user_cvs.parsing.domain.artifacts import DocumentArtifacts
from src.modules.user_cvs.parsing.domain.source import (
    EvidenceMapper,
    SourceBlock,
    SourceDocument,
    build_source_document,
)

__all__ = [
    "DocumentArtifacts",
    "EvidenceMapper",
    "SourceBlock",
    "SourceDocument",
    "build_source_document",
]
