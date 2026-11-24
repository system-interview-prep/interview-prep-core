from pydantic import BaseModel, Field


class SessionCloseRequest(BaseModel):
    note: str | None = Field(default=None, max_length=200)


class GradePendingRequest(BaseModel):
    userId: str | None = Field(default=None, max_length=36)
