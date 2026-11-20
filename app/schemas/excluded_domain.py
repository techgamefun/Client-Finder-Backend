import re
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.services.normalization import normalize_domain


ExcludedDomainReason = Literal[
    "Big Tech / Social",
    "Manual Exclusion",
    "Unreachable",
    "Blocked (403/429)",
]
ExcludedDomainSource = Literal["system_preset", "user", "auto_crawler"]

_DOMAIN_LABEL_PATTERN = re.compile(
    r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)


class ExcludedDomainCreate(BaseModel):
    domain: str = Field(min_length=1, max_length=2000)
    reason: ExcludedDomainReason
    note: str | None = Field(default=None, max_length=4000)

    @field_validator("domain", mode="before")
    @classmethod
    def normalize_and_validate_domain(cls, value: object) -> str:
        if not isinstance(value, str):
            raise ValueError("A website domain or URL is required.")
        domain = normalize_domain(value)
        if domain is None or len(domain) > 253:
            raise ValueError("Enter a valid website domain.")
        labels = domain.split(".")
        if len(labels) < 2 or any(
            not _DOMAIN_LABEL_PATTERN.fullmatch(label)
            for label in labels
        ):
            raise ValueError("Enter a valid website domain.")
        return domain

    @field_validator("note")
    @classmethod
    def trim_note(cls, value: str | None) -> str | None:
        return value.strip() or None if value is not None else None


class ExcludedDomainResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    domain: str
    reason: ExcludedDomainReason
    note: str | None
    source: ExcludedDomainSource
    failure_count: int
    created_at: datetime
    updated_at: datetime
