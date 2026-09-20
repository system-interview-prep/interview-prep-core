from pydantic import BaseModel, Field, field_validator
from src.modules.taxonomy.constants import CONCEPT_KINDS, RELATION_TYPES
class TaxonomyVersionUpsert(BaseModel):
    version: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{1,80}$")
    priority: int = 0
    activate: bool = False
class TaxonomyConceptUpsert(BaseModel):
    label: str = Field(min_length=1, max_length=256)
    kind: str = "skill"
    description: str | None = Field(default=None, max_length=4000)
    metadata: dict = Field(default_factory=dict)
    aliases: list[str] = Field(default_factory=list)
    isActive: bool = True
    @field_validator("kind")
    @classmethod
    def valid_kind(cls, value: str) -> str:
        if value not in CONCEPT_KINDS:
            raise ValueError("Unsupported taxonomy concept kind.")
        return value
class TaxonomyRelationUpsert(BaseModel):
    sourceConceptId: str = Field(min_length=1, max_length=256)
    targetConceptId: str = Field(min_length=1, max_length=256)
    relationType: str
    weight: float | None = Field(default=None, ge=0, le=1)
    isRequired: bool | None = None
    status: str = Field(default="ACTIVE", pattern="^(ACTIVE|RETIRED)$")
    @field_validator("relationType")
    @classmethod
    def valid_relation(cls, value: str) -> str:
        if value not in RELATION_TYPES:
            raise ValueError("Unsupported taxonomy relation type.")
        return value
