"""Public domain contracts and adapters for matching."""

from src.modules.matching.domain.adapters import job_description_to_matching_job
from src.modules.matching.domain.schemas import (
    CandidatePreferences,
    CanonicalJob,
    CompatibilityResult,
    ConceptResult,
    EvidenceCandidateTrace,
    FactorResult,
    GroundedJobText,
    LanguageRequirement,
    MatchAccepted,
    MatchRequest,
    MatchResult,
    MatchingPolicy,
    Requirement,
    RequirementResult,
    ScoreProvenance,
    SkillRequirement,
    UnresolvedRequirement,
)

__all__ = [
    "CandidatePreferences",
    "CanonicalJob",
    "CompatibilityResult",
    "ConceptResult",
    "EvidenceCandidateTrace",
    "FactorResult",
    "GroundedJobText",
    "LanguageRequirement",
    "MatchAccepted",
    "MatchRequest",
    "MatchResult",
    "MatchingPolicy",
    "Requirement",
    "RequirementResult",
    "ScoreProvenance",
    "SkillRequirement",
    "UnresolvedRequirement",
    "job_description_to_matching_job",
]
