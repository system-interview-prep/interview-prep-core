from typing import Literal

from pydantic import Field, model_validator

from src.modules.user_cvs.domain.schemas import (
    CanonicalModel,
    CareerClassification,
    EvidenceSpan,
    ParsingMetadata,
    TaxonomyRef,
)


class JobRequirement(CanonicalModel):
    requirement_id: str = Field(min_length=1)
    kind: Literal["skill", "experience", "education", "language", "other"]
    priority: Literal["must_have", "preferred"]
    concept: TaxonomyRef | None = None
    raw_label: str = Field(min_length=1)
    minimum_experience_months: int | None = Field(default=None, ge=0)
    evidence_refs: list[str] = Field(default_factory=list)


class GroundedJobText(CanonicalModel):
    text: str = Field(min_length=1)
    evidence_refs: list[str] = Field(default_factory=list)


class CanonicalJobDescription(CanonicalModel):
    schema_version: Literal["1.0"]
    job_title: str | None = None
    career_classifications: list[CareerClassification] = Field(default_factory=list)
    seniority: Literal["intern", "junior", "mid", "senior", "lead", "manager"] | None = None
    employment_type: Literal["full_time", "part_time", "contract", "internship"] | None = None
    work_mode: Literal["remote", "hybrid", "on_site"] | None = None
    location: str | None = None
    responsibilities: list[GroundedJobText] = Field(default_factory=list)
    requirements: list[JobRequirement] = Field(default_factory=list)
    benefits: list[GroundedJobText] = Field(default_factory=list)
    evidence: list[EvidenceSpan] = Field(default_factory=list)
    parsing: ParsingMetadata

    @model_validator(mode="after")
    def references_are_consistent(self) -> "CanonicalJobDescription":
        """Prevent review/finalize from persisting claims without document evidence."""
        evidence_ids = [item.evidence_id for item in self.evidence]
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("evidence IDs must be unique")
        requirement_ids = [item.requirement_id for item in self.requirements]
        if len(requirement_ids) != len(set(requirement_ids)):
            raise ValueError("requirement IDs must be unique")

        available = set(evidence_ids)
        owners = [
            *self.requirements,
            *self.responsibilities,
            *self.benefits,
            *self.career_classifications,
        ]
        if any(not set(owner.evidence_refs).issubset(available) for owner in owners):
            raise ValueError("all evidenceRefs must resolve inside the job description")
        return self
