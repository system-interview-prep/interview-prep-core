"""Compatibility facade for the shared career taxonomy."""

from src.modules.taxonomy.facade import (
    TAXONOMY_VERSION,
    CareerTaxonomyNode,
    career_taxonomy,
    classify_career,
)
from src.modules.user_cvs.domain.schemas import CareerClassification


class DeterministicCareerClassifier:
    def classify(self, *, skills, employment, headline=None, headline_evidence_refs=()):
        titles = [(entry.job_title, entry.evidence_refs) for entry in employment]
        if headline:
            titles.insert(0, (headline, list(headline_evidence_refs)))
        results = classify_career(
            {skill.concept.concept_id: skill.evidence_refs for skill in skills},
            titles,
            minimum_skill_signals=2,
            include_ancestors=True,
        )
        return [
            CareerClassification(
                code=item.code,
                label=item.label,
                dimension=item.dimension,
                taxonomyVersion=item.taxonomy_version,
                confidence=item.confidence,
                evidenceRefs=list(item.evidence_refs),
                isPrimary=item.is_primary,
            )
            for item in results
        ]


__all__ = [
    "TAXONOMY_VERSION",
    "CareerTaxonomyNode",
    "DeterministicCareerClassifier",
    "career_taxonomy",
]
