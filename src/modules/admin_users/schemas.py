from pydantic import BaseModel, Field, field_validator

from src.core.roles import USER_ROLES
from src.modules.auth.schemas import EMAIL_PATTERN, PASSWORD_PATTERN


def _validate_roles(value: list[str]) -> list[str]:
    normalized = [role.strip().upper() for role in value]
    if not normalized or len(normalized) != len(set(normalized)) or not set(normalized).issubset(USER_ROLES):
        raise ValueError("roles must be a non-empty, unique allow-listed set")
    return sorted(normalized)


class CreateAdminUserRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    email: str = Field(min_length=3, max_length=320)
    temporaryPassword: str = Field(min_length=8, max_length=128)
    roles: list[str] = Field(min_length=1)
    phone: str | None = Field(default=None, max_length=32)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        if not (value := value.strip()):
            raise ValueError("name must not be blank")
        return value

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        value = value.strip().lower()
        if not EMAIL_PATTERN.fullmatch(value):
            raise ValueError("invalid email")
        return value

    @field_validator("temporaryPassword")
    @classmethod
    def validate_temporary_password(cls, value: str) -> str:
        if not PASSWORD_PATTERN.fullmatch(value):
            raise ValueError("temporaryPassword does not meet password-strength requirements")
        return value

    @field_validator("roles")
    @classmethod
    def valid_roles(cls, value: list[str]) -> list[str]:
        return _validate_roles(value)


class ReplaceRolesRequest(BaseModel):
    roles: list[str] = Field(min_length=1)

    @field_validator("roles")
    @classmethod
    def valid_roles(cls, value: list[str]) -> list[str]:
        return _validate_roles(value)


class UserStatusRequest(BaseModel):
    isActive: bool
