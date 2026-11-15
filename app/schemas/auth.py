from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


Role = Literal["admin", "user"]


class CredentialsInput(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=12, max_length=256)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        normalized = value.strip().casefold()
        if normalized.count("@") != 1 or any(char.isspace() for char in normalized):
            raise ValueError("Enter a valid email address.")
        return normalized


class OrganizationRegistration(CredentialsInput):
    organization_name: str = Field(min_length=2, max_length=200)

    @field_validator("organization_name")
    @classmethod
    def normalize_organization_name(cls, value: str) -> str:
        normalized = value.strip()
        if len(normalized) < 2:
            raise ValueError("Organization name must have at least 2 characters.")
        return normalized


class OrganizationUserCreate(CredentialsInput):
    role: Role = "user"


class PasswordChange(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=12, max_length=256)


class AuthUserResponse(BaseModel):
    id: UUID
    email: str
    role: Role
    is_active: bool
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class AuthSessionResponse(BaseModel):
    user: AuthUserResponse
    organization_id: UUID
    organization_name: str


class OrganizationUsersResponse(BaseModel):
    users: list[AuthUserResponse]
