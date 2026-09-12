"""Public taxonomy contract for other modules."""

from src.modules.taxonomy.career import (
    TAXONOMY_VERSION,
    CareerClassificationResult,
    CareerTaxonomyNode,
    career_taxonomy,
    classify_career,
)

__all__ = [
    "TAXONOMY_VERSION",
    "CareerClassificationResult",
    "CareerTaxonomyNode",
    "career_taxonomy",
    "classify_career",
]
