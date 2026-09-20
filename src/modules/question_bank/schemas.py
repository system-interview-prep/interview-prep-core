"""Typed API contracts for controlled question-bank authoring."""

from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class TaxonomyMappingInput(BaseModel):
    concept_id: str = Field(alias="conceptId", min_length=1, max_length=256)
    purpose: str = Field(
        pattern="^(TARGET_ROLE|TARGET_SKILL|PRIMARY_COMPETENCY|SUPPORTING_COMPETENCY)$"
    )
    relevance: Decimal = Field(ge=0, le=1)


class CreateQuestionDraftRequest(BaseModel):
    stable_key: str = Field(alias="stableKey", min_length=3, max_length=180)
    version: str = Field(default="1.0.0", min_length=1, max_length=32)
    taxonomy_version: str = Field(alias="taxonomyVersion", min_length=1, max_length=80)
    question_type: str = Field(alias="questionType", min_length=1, max_length=48)
    difficulty_band: str = Field(alias="difficultyBand", min_length=1, max_length=32)
    canonical_locale: str = Field(alias="canonicalLocale", min_length=2, max_length=35)
    canonical_text: str = Field(alias="canonicalText", min_length=1)
    objective: str = Field(min_length=1)
    soft_answer_seconds: int = Field(alias="softAnswerSeconds", ge=1, le=7200)
    hard_answer_seconds: int = Field(alias="hardAnswerSeconds", ge=1, le=7200)
    thinking_seconds: int = Field(alias="thinkingSeconds", default=0, ge=0, le=3600)
    expert_difficulty: str | None = Field(alias="expertDifficulty", default=None, max_length=128)
    context_policy: dict = Field(alias="contextPolicy", default_factory=dict)
    personalization_policy: dict = Field(alias="personalizationPolicy", default_factory=dict)
    change_summary: str = Field(alias="changeSummary", default="")
    taxonomy_mappings: list[TaxonomyMappingInput] = Field(alias="taxonomyMappings", min_length=1)

    @field_validator("stable_key")
    @classmethod
    def normalize_stable_key(cls, value: str) -> str:
        return value.strip().lower()

    @field_validator("hard_answer_seconds")
    @classmethod
    def hard_limit_must_be_positive(cls, value: int) -> int:
        return value


class ReviewRequest(BaseModel):
    review_type: str = Field(alias="reviewType", min_length=1, max_length=32)
    decision: str = Field(pattern="^(APPROVE|REQUEST_CHANGES|REJECT|QUARANTINE)$")
    scores: dict = Field(default_factory=dict)
    checklist: dict = Field(default_factory=dict)
    findings: list[dict] = Field(default_factory=list)
    comment: str | None = None


class ApproveRequest(BaseModel):
    approval_policy_version: str = Field(alias="approvalPolicyVersion", min_length=1, max_length=32)
