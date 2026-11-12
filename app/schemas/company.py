from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator

from app.schemas.pagespeed import PageSpeedScanSummary


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


class ResearchSourceCrawlRequest(BaseModel):
    source_url: HttpUrl = Field(max_length=2000)


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
    name: str | None
    role: str | None
    business_email: str | None
    business_phone: str | None
    is_publicly_listed: bool


class LeadSourceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    source_url: str
    source_domain: str
    source_type: str | None
    evidence: str | None
    verified_at: datetime | None
    created_at: datetime


class CompanySearchKeywordResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    keyword: str
    searched_at: datetime


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
    contacts: list[ContactResponse]
    sources: list[LeadSourceResponse]
    search_keywords: list[CompanySearchKeywordResponse]
    pagespeed_scans: list[PageSpeedScanSummary] = Field(default_factory=list)
