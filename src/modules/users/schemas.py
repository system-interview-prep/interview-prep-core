from datetime import date

from pydantic import BaseModel, Field, field_validator


class ProfilePatch(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    dob: date | None = None

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("Họ và tên không được để trống.")
        return value


class CreditsResponse(BaseModel):
    cvScansRemaining: int
    mockSessionsRemaining: int


class ProfileResponse(BaseModel):
    id: str
    email: str
    name: str
    role: str
    provider: str
    dob: str | None
    picture: str | None
    avatar: str | None
    credits: CreditsResponse
    created_at: str
    createdAt: str
