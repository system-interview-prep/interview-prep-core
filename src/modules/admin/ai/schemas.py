from typing import Literal

from pydantic import BaseModel, Field


class TraceReviewRequest(BaseModel):
    name: str = Field(max_length=64)
    value: bool
    comment: str | None = Field(default=None, max_length=500)
    sessionId: str | None = Field(default=None, max_length=36)


class PlaybookReflectRequest(BaseModel):
    days: int = Field(default=14, ge=1, le=90)


class PlaybookEntryRequest(BaseModel):
    step: Literal["all", "evaluate_answer", "probe", "clarify"] = "all"
    text: str = Field(min_length=5, max_length=400)
    skill: str | None = Field(default=None, max_length=160)


class PlaybookDecisionRequest(BaseModel):
    decision: Literal["APPROVE", "REJECT", "RETIRE"]
    note: str | None = Field(default=None, max_length=500)
    text: str | None = Field(default=None, max_length=400)


class SpeechCheckReview(BaseModel):
    event_id: int = Field(alias="eventId", ge=1)
    check_index: int = Field(alias="checkIndex", ge=0, le=50)
    verdict: Literal["correct", "wrong", "clear"]


class AdminSpeechTestRequest(BaseModel):
    text: str = Field(min_length=1, max_length=4096)
