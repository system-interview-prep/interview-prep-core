import re
from typing import Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator

EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
PASSWORD_PATTERN = re.compile(r"^(?=.*[a-z])(?=.*[A-Z])(?=.*\d).{8,128}$")


class RegisterRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=8, max_length=128)
    phone: str | None = Field(default=None, max_length=32)
    role: Literal["CANDIDATE"] = "CANDIDATE"

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Họ và tên không được để trống.")
        return value

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        value = value.strip().lower()
        if not EMAIL_PATTERN.fullmatch(value):
            raise ValueError("Email không hợp lệ.")
        return value

    @field_validator("password")
    @classmethod
    def validate_password_strength(cls, value: str) -> str:
        if not PASSWORD_PATTERN.fullmatch(value):
            raise ValueError("Mật khẩu phải có ít nhất 8 ký tự, gồm chữ hoa, chữ thường và số.")
        return value

    @field_validator("phone")
    @classmethod
    def normalize_phone(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=128)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        return RegisterRequest.normalize_email(value)


class GoogleLoginRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    token: str = Field(min_length=1, validation_alias=AliasChoices("token", "accessToken"))

    @field_validator("token", mode="before")
    @classmethod
    def strip_token(cls, value: str) -> str:
        return value.strip()

    @property
    def accessToken(self) -> str:  # noqa: N802 - compatibility with the previous frontend contract
        return self.token


class PublicUserResponse(BaseModel):
    id: str
    email: str
    name: str
    role: Literal["CANDIDATE", "ADMIN"]
    provider: str
    picture: str | None = None
    avatar: str | None = None


class AuthResponse(BaseModel):
    access_token: str
    user: PublicUserResponse
