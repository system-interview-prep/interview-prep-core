from pydantic import BaseModel, Field, field_validator
from src.core.roles import USER_ROLES

class CreateAdminUserRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    email: str = Field(min_length=3, max_length=320)
    temporaryPassword: str = Field(min_length=8, max_length=128)
    roles: list[str] = Field(min_length=1)
    phone: str | None = Field(default=None, max_length=32)
    @field_validator("roles")
    @classmethod
    def valid_roles(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)) or not set(value).issubset(USER_ROLES): raise ValueError("roles must be a unique allow-listed set")
        return sorted(value)
class ReplaceRolesRequest(BaseModel):
    roles: list[str] = Field(min_length=1)
    @field_validator("roles")
    @classmethod
    def valid_roles(cls, value: list[str]) -> list[str]:
        return CreateAdminUserRequest.valid_roles(value)
class UserStatusRequest(BaseModel):
    isActive: bool
