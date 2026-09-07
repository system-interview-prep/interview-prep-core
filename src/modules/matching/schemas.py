from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class MatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resume_text: str = Field(min_length=1)
    job_description: str = Field(min_length=1)
    cv_id: str | None = None
    job_id: str | None = None
    position: str | None = None
    algorithms: list[str] = Field(
        default_factory=lambda: ["embedding_cosine"],
        description="Deprecated compatibility field; matching always uses embedding cosine.",
    )
    async_processing: bool = True


class MatchAccepted(BaseModel):
    task_id: str
    status: str = "PENDING"


class MatchResult(BaseModel):
    pipeline_version: str = "external-embedding-cosine-v1"
    result: dict[str, Any]
