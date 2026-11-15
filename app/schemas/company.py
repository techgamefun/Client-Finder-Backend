import re
from datetime import datetime
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    field_validator,
    model_validator,
)

from app.schemas.pagespeed import PageSpeedScanSummary


CONTACT_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s.]+(?:\.[^@\s.]+)+$")


def _trim_contact_text(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def _validate_contact_email(value: str | None) -> str | None:
    if value is not None and not CONTACT_EMAIL_PATTERN.fullmatch(value):
        raise ValueError("Enter a valid business email address.")
    return value


class ContactCreate(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    role: str | None = Field(default=None, max_length=150)
    business_email: str | None = Field(default=None, max_length=320)
    business_phone: str | None = Field(default=None, max_length=50)
    is_publicly_listed: bool = False

    @field_validator("name", "role", "business_email", "business_phone")
    @classmethod
    def trim_optional_fields(cls, value: str | None) -> str | None:
        return _trim_contact_text(value)

    @field_validator("business_email")
    @classmethod
    def validate_business_email(cls, value: str | None) -> str | None:
        return _validate_contact_email(value)

    @model_validator(mode="after")
    def require_contact_details(self) -> "ContactCreate":
        if not any(
            (
                self.name,
                self.role,
                self.business_email,
                self.business_phone,
            )
        ):
            raise ValueError("Provide at least one contact detail.")
        return self


class ContactUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    role: str | None = Field(default=None, max_length=150)
    business_email: str | None = Field(default=None, max_length=320)
    business_phone: str | None = Field(default=None, max_length=50)
    is_publicly_listed: bool | None = None

    @field_validator("name", "role", "business_email", "business_phone")
    @classmethod
    def trim_optional_fields(cls, value: str | None) -> str | None:
        return _trim_contact_text(value)

    @field_validator("business_email")
    @classmethod
    def validate_business_email(cls, value: str | None) -> str | None:
        return _validate_contact_email(value)

    @model_validator(mode="after")
    def require_changes(self) -> "ContactUpdate":
        if not self.model_fields_set:
            raise ValueError("At least one contact field must be provided.")
        if "is_publicly_listed" in self.model_fields_set:
            if self.is_publicly_listed is None:
                raise ValueError("is_publicly_listed cannot be null.")
        return self


class CompanyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    website: HttpUrl | None = None
    domain: str | None = Field(default=None, max_length=255)
    industry: str | None = Field(default=None, max_length=150)
    city: str | None = Field(default=None, max_length=100)
    state: str | None = Field(default=None, max_length=100)
    software_need: str | None = None


class CompanyUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    website: HttpUrl | None = None
    domain: str | None = Field(default=None, max_length=255)
    industry: str | None = Field(default=None, max_length=150)
    city: str | None = Field(default=None, max_length=100)
    state: str | None = Field(default=None, max_length=100)
    software_need: str | None = None
    lead_score: int | None = Field(default=None, ge=0)


class LeadStatusUpdate(BaseModel):
    lead_status: str = Field(min_length=1, max_length=30)

    @field_validator("lead_status")
    @classmethod
    def trim_lead_status(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("lead_status cannot be blank.")
        return value


class ContactResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    company_id: UUID
    name: str | None
    role: str | None
    business_email: str | None
    business_phone: str | None
    is_publicly_listed: bool


class CompanyResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    website: str | None
    domain: str | None
    industry: str | None
    city: str | None
    state: str | None
    software_need: str | None
    lead_score: int | None
    lead_status: str
    created_at: datetime
    created_by_email: str | None = None
    contacts: list[ContactResponse]
    pagespeed_scans: list[PageSpeedScanSummary] = Field(default_factory=list)
