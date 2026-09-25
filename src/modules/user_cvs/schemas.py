"""Public canonical CV schema contract for other feature modules."""

from src.modules.user_cvs.domain.schemas import (
    CanonicalModel,
    CanonicalResume,
    CareerClassification,
    EvidenceSpan,
    ParserWarning,
    ParsingMetadata,
    ProficiencyLevel,
    TaxonomyRef,
)

__all__ = [
    "CanonicalModel",
    "CanonicalResume",
    "CareerClassification",
    "EvidenceSpan",
    "ParserWarning",
    "ParsingMetadata",
    "ProficiencyLevel",
    "TaxonomyRef",
]
