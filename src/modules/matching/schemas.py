from typing import Annotated, Literal

from pydantic import Field, model_validator

from src.modules.user_cvs.schemas import (
    CanonicalModel,
    CanonicalResume,
    CareerClassification,
    EvidenceSpan,
    ProficiencyLevel,
    TaxonomyRef,
)


class RequirementBase(CanonicalModel):
    requirement_id: str = Field(min_length=1)
    priority: Literal["must_have", "nice_to_have", "context"]
    source_evidence_ref: str = Field(min_length=1)


class SkillRequirement(RequirementBase):
    type: Literal["skill"]
    skill: TaxonomyRef
    operator: Literal["required", "gte", "proficiency_gte"] = "required"
    minimum_experience_months: int | None = Field(default=None, ge=0)
    minimum_proficiency_level: ProficiencyLevel | None = None

    @model_validator(mode="after")
    def experience_operator_has_value(self) -> "SkillRequirement":
        if self.operator == "gte" and self.minimum_experience_months is None:
            raise ValueError("gte skill requirements need minimumExperienceMonths")
        if self.operator != "gte" and self.minimum_experience_months is not None:
            raise ValueError("only gte skill requirements may set minimumExperienceMonths")
        if self.operator == "proficiency_gte" and self.minimum_proficiency_level is None:
            raise ValueError("proficiency_gte requirements need minimumProficiencyLevel")
        if self.operator != "proficiency_gte" and self.minimum_proficiency_level is not None:
            raise ValueError("only proficiency_gte requirements may set minimumProficiencyLevel")
        return self


class LanguageRequirement(RequirementBase):
    type: Literal["language"]
    language_code: str = Field(pattern=r"^[a-z]{2,3}$")
    operator: Literal["required", "equal"] = "required"
    minimum_level: str | None = None

    @model_validator(mode="after")
    def level_operator_has_value(self) -> "LanguageRequirement":
        if self.operator == "equal" and not self.minimum_level:
            raise ValueError("equal language requirements need minimumLevel")
        return self


class UnresolvedRequirement(RequirementBase):
    """Evidence-grounded JD requirement that needs a specialized evaluator."""

    type: Literal["unresolved"]
    kind: Literal["skill", "experience", "education", "language", "other"]
    raw_label: str = Field(min_length=1)
    atomic_concepts: list[TaxonomyRef] = Field(default_factory=list)
    minimum_experience_months: int | None = Field(default=None, ge=0)
    operator: Literal["gte", "gt", "lte", "lt", "eq", "required"] | None = None
    threshold: float | None = None
    scale: float | None = None
    credential: str | None = None
    equivalent_allowed: bool | None = None
    group_id: str | None = None
    group_operator: Literal["all_of", "any_of", "atomic"] | None = None

    @model_validator(mode="after")
    def atomic_group_is_consistent(self) -> "UnresolvedRequirement":
        concept_ids = [item.concept_id for item in self.atomic_concepts]
        if len(concept_ids) != len(set(concept_ids)):
            raise ValueError("atomicConcepts must be unique")
        if len(self.atomic_concepts) > 1 and self.group_operator not in {"all_of", "any_of"}:
            raise ValueError("multi-concept requirements need all_of or any_of")
        return self


Requirement = Annotated[
    SkillRequirement | LanguageRequirement | UnresolvedRequirement,
    Field(discriminator="type"),
]


class GroundedJobText(CanonicalModel):
    text: str = Field(min_length=1)
    evidence_refs: list[str] = Field(default_factory=list)


class CanonicalJob(CanonicalModel):
    schema_version: Literal["2.1"]
    job_id: str = Field(min_length=1)
    job_version_id: str | None = None
    document_id: str = Field(min_length=1)
    document_sha256: str = Field(min_length=64, max_length=64, pattern=r"^[a-fA-F0-9]+$")
    job_title: str | None = None
    career_classifications: list[CareerClassification] = Field(default_factory=list)
    seniority: (
        Literal["intern", "fresher", "junior", "mid", "senior", "lead", "manager"] | None
    ) = None
    employment_type: (
        Literal["full_time", "part_time", "contract", "internship", "temporary"] | None
    ) = None
    work_mode: Literal["remote", "hybrid", "on_site"] | None = None
    location: str | None = None
    responsibilities: list[GroundedJobText] = Field(default_factory=list)
    benefits: list[GroundedJobText] = Field(default_factory=list)
    requirements: list[Requirement] = Field(default_factory=list)
    evidence: list[EvidenceSpan] = Field(default_factory=list)

    @model_validator(mode="after")
    def references_are_consistent(self) -> "CanonicalJob":
        evidence_ids = {item.evidence_id for item in self.evidence}
        if len(evidence_ids) != len(self.evidence):
            raise ValueError("evidence IDs must be unique")
        if not {item.source_evidence_ref for item in self.requirements}.issubset(evidence_ids):
            raise ValueError("all requirement sourceEvidenceRefs must resolve inside the job")
        grounded_owners = [
            *self.responsibilities,
            *self.benefits,
            *self.career_classifications,
        ]
        if any(not set(owner.evidence_refs).issubset(evidence_ids) for owner in grounded_owners):
            raise ValueError("all job evidenceRefs must resolve inside the job")
        if any(
            item.document_id != self.document_id or item.document_sha256 != self.document_sha256
            for item in self.evidence
        ):
            raise ValueError("evidence must belong to the job document revision")
        return self


class MatchingPolicy(CanonicalModel):
    policy_version: Literal["balanced-v1", "skill-focus-v1", "experience-focus-v1"] = "balanced-v1"
    must_have_mode: Literal["strict", "advisory"] = "strict"
    # An ungrounded must-have is not evidence of fit.  Require manual review
    # instead of allowing semantic similarity to promote the match.
    unknown_handling: Literal["manual_review", "penalize"] = "manual_review"
    semantic_mode: Literal["hybrid", "dense_only", "sparse_only"] = "hybrid"
    bm25_weight: float = Field(default=0.4, ge=0.0, le=1.0)
    bm25_provider_mode: Literal["auto", "in_memory", "paradedb"] = "auto"


class CandidatePreferences(CanonicalModel):
    """Explicit user choices; these are not facts extracted from the CV."""

    accepted_work_modes: list[Literal["remote", "hybrid", "on_site"]] = Field(
        default_factory=list
    )
    accepted_locations: list[str] = Field(default_factory=list)
    willing_to_relocate: bool | None = None


class MatchRequest(CanonicalModel):
    """The sole public contract for one CV-to-one-JD fit assessment."""

    schema_version: Literal["2.1"]
    resume: CanonicalResume
    job: CanonicalJob
    matching_policy: MatchingPolicy = Field(default_factory=MatchingPolicy)
    candidate_preferences: CandidatePreferences = Field(default_factory=CandidatePreferences)
    async_processing: bool = True


class MatchAccepted(CanonicalModel):
    task_id: str
    status: Literal["PENDING"] = "PENDING"


class RequirementResult(CanonicalModel):
    requirement_id: str
    status: Literal["met", "not_met", "unknown", "not_applicable"]
    score: float | None = Field(default=None, ge=0, le=1)
    confidence: float = Field(ge=0, le=1)
    evidence_refs: list[str] = Field(default_factory=list)
    reason_code: str
    evidence_explanation: str | None = None
    concept_results: list["ConceptResult"] = Field(default_factory=list)
    group_operator: Literal["atomic", "all_of", "any_of"] = "atomic"

    @model_validator(mode="after")
    def requirement_evidence_is_concept_union(self) -> "RequirementResult":
        if self.concept_results:
            expected = list(
                dict.fromkeys(
                    ref
                    for concept in self.concept_results
                    for ref in concept.evidence_refs
                )
            )
            if self.evidence_refs != expected:
                raise ValueError("evidenceRefs must be the ordered union of conceptResults evidenceRefs")
        if self.status == "unknown" and not self.evidence_explanation:
            self.evidence_explanation = (
                f"Chưa có bằng chứng đủ rõ để kết luận; nguyên nhân đánh giá: {self.reason_code}."
            )
        return self


class ConceptResult(CanonicalModel):
    concept_id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    status: Literal["met", "not_met", "unknown", "not_applicable"]
    confidence: float = Field(ge=0, le=1)
    evidence_refs: list[str] = Field(default_factory=list)
    evidence_strength: Literal["mention", "claimed", "applied", "demonstrated"] | None = None
    reason_code: str


class FactorResult(CanonicalModel):
    factor: Literal["requirement_coverage", "skill", "experience", "language", "semantic"]
    status: Literal["scored", "not_applicable", "unknown"]
    raw_score: float | None = Field(default=None, ge=0, le=1)
    reliability: float = Field(ge=0, le=1)
    policy_weight: float = Field(ge=0, le=1)
    effective_weight: float = Field(ge=0, le=1)
    evidence_refs: list[str] = Field(default_factory=list)
    warning_code: str | None = None
    dense_score: float | None = Field(default=None, ge=0, le=1)
    sparse_score: float | None = Field(default=None, ge=0, le=1)


class CompatibilityResult(CanonicalModel):
    criterion: Literal["work_mode", "location"]
    status: Literal["compatible", "incompatible", "unknown", "not_applicable"]
    confidence: float = Field(ge=0, le=1)
    reason_code: str


class ScoreProvenance(CanonicalModel):
    mode: Literal["requirement_aware", "semantic_only_estimated", "unavailable"]
    scored_factors: list[str] = Field(default_factory=list)
    supported_requirement_count: int = Field(ge=0)
    scored_requirement_count: int = Field(ge=0)
    unknown_requirement_count: int = Field(default=0, ge=0)
    total_requirement_count: int = Field(ge=0)
    requirement_coverage: float | None = Field(default=None, ge=0, le=1)
    factor_contributions: dict[str, float] = Field(default_factory=dict)


class MatchResult(CanonicalModel):
    schema_version: Literal["2.1"] = "2.1"
    pipeline_version: Literal["one-to-one-evidence-fusion-v1"] = "one-to-one-evidence-fusion-v1"
    resume_id: str
    job_id: str
    job_version_id: str | None = None
    policy_version: str
    eligibility: Literal["eligible", "ineligible", "review_required"]
    compatibility_status: Literal[
        "compatible", "incompatible", "unknown", "not_applicable"
    ] = "not_applicable"
    suitability_score: float | None = Field(default=None, ge=0, le=1)
    fit_band: Literal["strong_fit", "partial_fit", "review_required", "not_eligible", "insufficient_evidence"]
    decision: Literal["assessed", "abstained"]
    requirement_results: list[RequirementResult]
    compatibility_results: list[CompatibilityResult] = Field(default_factory=list)
    factor_results: list[FactorResult]
    score_provenance: ScoreProvenance = Field(
        default_factory=lambda: ScoreProvenance(
            mode="unavailable",
            supportedRequirementCount=0,
            scoredRequirementCount=0,
            totalRequirementCount=0,
        )
    )
    warnings: list[str] = Field(default_factory=list)
