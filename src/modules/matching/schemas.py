from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.modules.user_cvs.domain.schemas import CanonicalModel, CanonicalResume, EvidenceSpan, TaxonomyRef


def _to_camel(name: str) -> str:
    head, *tail = name.split("_")
    return head + "".join(part.capitalize() for part in tail)


class MatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", alias_generator=_to_camel)

    resume_text: str = Field(min_length=1)
    job_description: str = Field(min_length=1)
    cv_id: str | None = None
    job_description_id: str | None = None
    position: str | None = None
    algorithms: list[str] = Field(
        default_factory=lambda: ["embedding_cosine"],
        description="Deprecated compatibility field; matching always uses embedding cosine.",
    )
    async_processing: bool = True


class MatchAccepted(BaseModel):
    model_config = ConfigDict(alias_generator=_to_camel)

    task_id: str
    status: str = "PENDING"


class MatchResult(BaseModel):
    model_config = ConfigDict(alias_generator=_to_camel)

    pipeline_version: str = "external-embedding-cosine-v1"
    result: dict[str, Any]


class RequirementBase(CanonicalModel):
    requirement_id: str = Field(min_length=1)
    priority: Literal["must_have", "nice_to_have", "context"]
    source_evidence_ref: str = Field(min_length=1)


class SkillRequirement(RequirementBase):
    type: Literal["skill"]
    skill: TaxonomyRef
    operator: Literal["required", "gte"] = "required"
    minimum_experience_months: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def experience_operator_has_value(self) -> "SkillRequirement":
        if self.operator == "gte" and self.minimum_experience_months is None:
            raise ValueError("gte skill requirements need minimumExperienceMonths")
        if self.operator == "required" and self.minimum_experience_months is not None:
            raise ValueError("required skill requirements must not set minimumExperienceMonths")
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


Requirement = Annotated[Union[SkillRequirement, LanguageRequirement], Field(discriminator="type")]


class CanonicalJob(CanonicalModel):
    schema_version: Literal["2.1"]
    job_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    document_sha256: str = Field(min_length=64, max_length=64, pattern=r"^[a-fA-F0-9]+$")
    requirements: list[Requirement] = Field(default_factory=list)
    evidence: list[EvidenceSpan] = Field(default_factory=list)


class MatchingPolicy(CanonicalModel):
    policy_version: str = Field(min_length=1)
    must_have_mode: Literal["strict", "advisory"] = "strict"
    unknown_handling: Literal["manual_review", "penalize"] = "manual_review"


class StructuredMatchRequest(CanonicalModel):
    schema_version: Literal["2.1"]
    resume: CanonicalResume
    job: CanonicalJob
    matching_policy: MatchingPolicy


class RequirementResult(CanonicalModel):
    requirement_id: str
    status: Literal["met", "not_met", "unknown", "not_applicable"]
    score: float | None = Field(default=None, ge=0, le=1)
    evidence_refs: list[str] = Field(default_factory=list)
    reason_code: str


class StructuredMatchResult(CanonicalModel):
    schema_version: Literal["2.1"]
    resume_id: str
    job_id: str
    policy_version: str
    overall_score: float = Field(ge=0, le=1)
    recommendation: Literal["strong_match", "review", "not_match"]
    requirement_results: list[RequirementResult]
