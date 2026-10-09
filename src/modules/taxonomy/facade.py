"""Public taxonomy contract for other modules."""

from src.modules.taxonomy.career import (
    TAXONOMY_VERSION,
    CareerClassificationResult,
    CareerTaxonomyNode,
    career_taxonomy,
    classify_career,
)
from src.modules.taxonomy.service import TaxonomySnapshot, load_active_skill_taxonomy
from src.modules.taxonomy.skill_catalog import (
    SKILL_CATALOG,
    SkillEntry,
    alias_pattern,
    role_skill_ids,
    skill_aliases,
)

__all__ = [
    "SKILL_CATALOG",
    "TAXONOMY_VERSION",
    "CareerClassificationResult",
    "CareerTaxonomyNode",
    "SkillEntry",
    "TaxonomySnapshot",
    "alias_pattern",
    "career_taxonomy",
    "classify_career",
    "load_active_skill_taxonomy",
    "role_skill_ids",
    "skill_aliases",
]
