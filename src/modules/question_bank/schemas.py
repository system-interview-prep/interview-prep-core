"""Typed API contracts for controlled question-bank authoring."""

from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

# Vocabularies the P2 selector actually understands. Anything else is filtered
# out at selection time (difficulty distance 99), so reject it at authoring.
DIFFICULTY_BANDS = ("foundational", "intermediate", "advanced")
QUESTION_TYPES = ("technical", "conceptual", "system_design", "coding")
# Taxonomy version that holds the skill and specialization concepts. The P2
# selector bridges it to the `internal-2026.1` version stamped by the parsers.
DEFAULT_TAXONOMY_VERSION = "internal-career-2026.1"


def normalize_vocabulary(value: str, allowed: tuple[str, ...], field_name: str) -> str:
    normalized = value.strip().lower()
    if normalized not in allowed:
        raise ValueError(f"{field_name} must be one of: {', '.join(allowed)}.")
    return normalized


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

    @field_validator("question_type")
    @classmethod
    def normalize_question_type(cls, value: str) -> str:
        return normalize_vocabulary(value, QUESTION_TYPES, "questionType")

    @field_validator("difficulty_band")
    @classmethod
    def normalize_difficulty_band(cls, value: str) -> str:
        return normalize_vocabulary(value, DIFFICULTY_BANDS, "difficultyBand")

    @field_validator("hard_answer_seconds")
    @classmethod
    def hard_limit_must_be_positive(cls, value: int) -> int:
        return value


class UpdateQuestionDraftRequest(BaseModel):
    """Partial edit of a DRAFT / NEEDS_REVISION version by its author."""

    canonical_text: str | None = Field(alias="canonicalText", default=None, min_length=1)
    objective: str | None = Field(default=None, min_length=1)
    question_type: str | None = Field(alias="questionType", default=None)
    difficulty_band: str | None = Field(alias="difficultyBand", default=None)
    thinking_seconds: int | None = Field(alias="thinkingSeconds", default=None, ge=0, le=3600)
    soft_answer_seconds: int | None = Field(alias="softAnswerSeconds", default=None, ge=1, le=7200)
    hard_answer_seconds: int | None = Field(alias="hardAnswerSeconds", default=None, ge=1, le=7200)
    change_summary: str | None = Field(alias="changeSummary", default=None)
    taxonomy_mappings: list[TaxonomyMappingInput] | None = Field(
        alias="taxonomyMappings", default=None, min_length=1
    )

    @field_validator("question_type")
    @classmethod
    def normalize_question_type(cls, value: str | None) -> str | None:
        return None if value is None else normalize_vocabulary(value, QUESTION_TYPES, "questionType")

    @field_validator("difficulty_band")
    @classmethod
    def normalize_difficulty_band(cls, value: str | None) -> str | None:
        return None if value is None else normalize_vocabulary(value, DIFFICULTY_BANDS, "difficultyBand")


class RubricAnchorInput(BaseModel):
    level: int = Field(ge=0, le=3)
    description: str = Field(min_length=1)


class RubricCriterionInput(BaseModel):
    stable_key: str = Field(alias="stableKey", min_length=1, max_length=120)
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    weight: Decimal = Field(gt=0, le=1)
    critical: bool = False
    anchors: list[RubricAnchorInput] = Field(min_length=4, max_length=4)

    @field_validator("anchors")
    @classmethod
    def anchors_cover_every_level(cls, value: list[RubricAnchorInput]) -> list[RubricAnchorInput]:
        if sorted(anchor.level for anchor in value) != [0, 1, 2, 3]:
            raise ValueError("Each criterion needs exactly one anchor per level 0-3.")
        return value


class AttachRubricRequest(BaseModel):
    """Attach a rubric to a draft: link an existing rubric version, or define one inline."""

    rubric_version_id: UUID | None = Field(alias="rubricVersionId", default=None)
    minimum_coverage: Decimal = Field(alias="minimumCoverage", default=Decimal("0.6"), ge=0, le=1)
    criteria: list[RubricCriterionInput] = Field(default_factory=list)


class ReviewRequest(BaseModel):
    review_type: str = Field(alias="reviewType", min_length=1, max_length=32)
    decision: str = Field(pattern="^(APPROVE|REQUEST_CHANGES|REJECT|QUARANTINE)$")
    scores: dict = Field(default_factory=dict)
    checklist: dict = Field(default_factory=dict)
    findings: list[dict] = Field(default_factory=list)
    comment: str | None = None


class ApproveRequest(BaseModel):
    approval_policy_version: str = Field(alias="approvalPolicyVersion", min_length=1, max_length=32)
