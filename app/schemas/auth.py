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
    organization_description: str | None = Field(default=None, max_length=1000)
    first_name: str = Field(min_length=1, max_length=100)
    last_name: str = Field(min_length=1, max_length=100)

    @field_validator("organization_name")
    @classmethod
    def normalize_organization_name(cls, value: str) -> str:
        normalized = value.strip()
        if len(normalized) < 2:
            raise ValueError("Organization name must have at least 2 characters.")
        return normalized

    @field_validator("first_name", "last_name")
    @classmethod
    def normalize_registration_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("First name and last name are required.")
        return normalized


class OrganizationUserCreate(CredentialsInput):
    first_name: str | None = Field(default=None, max_length=100)
    last_name: str | None = Field(default=None, max_length=100)
    role: Role = "user"

    @field_validator("first_name", "last_name")
    @classmethod
    def normalize_person_name(cls, value: str | None) -> str | None:
        normalized = value.strip() if value is not None else None
        return normalized or None


class OrganizationUserRoleUpdate(BaseModel):
    role: Role


class OrganizationProfileUpdate(BaseModel):
    description: str | None = Field(max_length=1000)

    @field_validator("description")
    @classmethod
    def normalize_description(cls, value: str | None) -> str | None:
        normalized = value.strip() if value is not None else None
        return normalized or None


class AccountProfileUpdate(BaseModel):
    first_name: str = Field(min_length=1, max_length=100)
    last_name: str = Field(min_length=1, max_length=100)

    @field_validator("first_name", "last_name")
    @classmethod
    def normalize_account_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("First name and last name are required.")
        return normalized


class OrganizationProfileResponse(BaseModel):
    description: str | None


class PasswordChange(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=12, max_length=256)


class AuthUserResponse(BaseModel):
    id: UUID
    email: str
    first_name: str | None
    last_name: str | None
    role: Role
    is_active: bool
    created_at: datetime
    profile_photo_url: str | None

    model_config = ConfigDict(from_attributes=True)


class AuthSessionResponse(BaseModel):
    user: AuthUserResponse
    organization_id: UUID
    organization_name: str
    organization_description: str | None
    organization_logo_url: str | None
    organization_banner_url: str | None


class OrganizationUsersResponse(BaseModel):
    users: list[AuthUserResponse]
