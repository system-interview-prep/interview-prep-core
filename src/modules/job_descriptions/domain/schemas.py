from typing import Literal

from pydantic import Field, model_validator

from src.modules.user_cvs.schemas import (
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
    atomic_concepts: list[TaxonomyRef] = Field(default_factory=list)
    raw_label: str = Field(min_length=1)
    minimum_experience_months: int | None = Field(default=None, ge=0)
    group_id: str | None = None
    group_operator: Literal["all_of", "any_of", "atomic"] | None = None
    operator: Literal["gte", "gt", "lte", "lt", "eq", "required"] | None = None
    threshold: float | None = None
    scale: float | None = None
    credential: str | None = None
    equivalent_allowed: bool | None = None
    evidence_refs: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def atomic_group_is_consistent(self) -> "JobRequirement":
        concept_ids = [item.concept_id for item in self.atomic_concepts]
        if len(concept_ids) != len(set(concept_ids)):
            raise ValueError("atomicConcepts must be unique")
        if self.atomic_concepts and self.concept is not None:
            raise ValueError("concept and atomicConcepts are mutually exclusive")
        if len(self.atomic_concepts) > 1 and self.group_operator not in {"all_of", "any_of"}:
            raise ValueError("multi-concept requirements need all_of or any_of")
        return self


class GroundedJobText(CanonicalModel):
    text: str = Field(min_length=1)
    evidence_refs: list[str] = Field(default_factory=list)


class CanonicalJobDescription(CanonicalModel):
    schema_version: Literal["1.0"]
    job_title: str | None = None
    company_name: str | None = None
    career_classifications: list[CareerClassification] = Field(default_factory=list)
    seniority: (
        Literal["intern", "fresher", "junior", "mid", "senior", "lead", "manager"] | None
    ) = None
    employment_type: (
        Literal["full_time", "part_time", "internship", "contract", "temporary"] | None
    ) = None
    work_mode: Literal["remote", "hybrid", "on_site"] | None = None
    location: str | None = None
    experience_min_years: int | None = Field(default=None, ge=0)
    experience_max_years: int | None = Field(default=None, ge=0)
    experience_raw: str | None = None
    salary_min: int | None = Field(default=None, ge=0)
    salary_max: int | None = Field(default=None, ge=0)
    salary_currency: str | None = None
    salary_period: Literal["hour", "month", "year"] | None = None
    salary_negotiable: bool | None = None
    salary_raw: str | None = None
    responsibilities: list[GroundedJobText] = Field(default_factory=list)
    requirements: list[JobRequirement] = Field(default_factory=list)
    benefits: list[GroundedJobText] = Field(default_factory=list)
    evidence: list[EvidenceSpan] = Field(default_factory=list)
    parsing: ParsingMetadata

    @model_validator(mode="after")
    def references_and_ranges_are_consistent(self) -> "CanonicalJobDescription":
        """Validate evidence integrity and numerical ranges."""
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

        if (
            self.experience_min_years is not None
            and self.experience_max_years is not None
            and self.experience_min_years > self.experience_max_years
        ):
            raise ValueError("experience_min_years cannot exceed experience_max_years")

        if (
            self.salary_min is not None
            and self.salary_max is not None
            and self.salary_min > self.salary_max
        ):
            raise ValueError("salary_min cannot exceed salary_max")

        has_numeric_salary = self.salary_min is not None or self.salary_max is not None
        if has_numeric_salary:
            if not self.salary_currency or not self.salary_currency.strip():
                raise ValueError("salary_currency is required when numeric salary is specified")
            if not self.salary_period:
                raise ValueError("salary_period is required when numeric salary is specified")

        return self
