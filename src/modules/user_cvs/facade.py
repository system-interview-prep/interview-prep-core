"""Public document-source contract reused by evidence-grounded consumers."""

from src.modules.user_cvs.application.admin_reparse import CvAdminReparseError, reparse_cv_as_admin
from src.modules.user_cvs.parsing.domain.artifacts import DocumentArtifacts
from src.modules.user_cvs.parsing.domain.deterministic import candidate_name_from_text
from src.modules.user_cvs.parsing.domain.source import (
    EvidenceMapper,
    SourceBlock,
    SourceDocument,
    build_source_document,
)

__all__ = [
    "CvAdminReparseError",
    "DocumentArtifacts",
    "EvidenceMapper",
    "SourceBlock",
    "SourceDocument",
    "build_source_document",
    "candidate_name_from_text",
    "reparse_cv_as_admin",
]
