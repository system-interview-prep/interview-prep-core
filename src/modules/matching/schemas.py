from typing import Any

from pydantic import BaseModel, ConfigDict, Field


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
