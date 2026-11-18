from datetime import datetime
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class EmailCampaignFollowUpStep(BaseModel):
    template_id: UUID
    delay_days: int = Field(ge=1, le=365)


class EmailCampaignCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    template_id: UUID
    follow_up_steps: list[EmailCampaignFollowUpStep] = Field(
        default_factory=list,
        max_length=10,
    )
    verify_mx: bool = True
    gmail_account_ids: list[UUID] = Field(min_length=1, max_length=50)
    daily_limit_per_account: int = Field(default=40, ge=1, le=1000)
    min_delay_seconds: int = Field(default=180, ge=1, le=86_400)
    max_delay_seconds: int = Field(default=420, ge=1, le=86_400)
    send_window_start_hour: int = Field(default=9, ge=0, le=22)
    send_window_end_hour: int = Field(default=17, ge=1, le=23)
    timezone: str = Field(default="UTC", min_length=1, max_length=64)

    @field_validator("name")
    @classmethod
    def name_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Campaign name cannot be blank.")
        return value.strip()

    @field_validator("gmail_account_ids")
    @classmethod
    def account_ids_must_be_unique(cls, value: list[UUID]) -> list[UUID]:
        if len(set(value)) != len(value):
            raise ValueError("Gmail account IDs must be unique.")
        return value

    @field_validator("timezone")
    @classmethod
    def timezone_must_be_valid(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("Timezone must be a valid IANA timezone.") from exc
        return value

    @model_validator(mode="after")
    def validate_campaign_settings(self) -> "EmailCampaignCreate":
        if self.min_delay_seconds > self.max_delay_seconds:
            raise ValueError("Minimum delay cannot exceed maximum delay.")
        if self.send_window_start_hour >= self.send_window_end_hour:
            raise ValueError("Send window start must be before its end.")
        return self


class EmailCampaignPreviewRequest(BaseModel):
    company_ids: list[UUID] = Field(min_length=1, max_length=5000)

    @field_validator("company_ids")
    @classmethod
    def company_ids_must_be_unique(cls, value: list[UUID]) -> list[UUID]:
        if len(set(value)) != len(value):
            raise ValueError("Company IDs must be unique.")
        return value


class EmailCampaignRecipientCounts(BaseModel):
    pending: int = 0
    processing: int = 0
    sent: int = 0
    failed: int = 0
    cancelled: int = 0
    replied: int = 0
    bounced: int = 0
    sent_messages: int = 0
    unverified: int = 0


class EmailCampaignResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    org_id: UUID
    created_by: UUID
    name: str
    template_id: UUID
    follow_up_steps: list[EmailCampaignFollowUpStep]
    verify_mx: bool
    gmail_account_ids: list[UUID]
    daily_limit_per_account: int
    min_delay_seconds: int
    max_delay_seconds: int
    send_window_start_hour: int
    send_window_end_hour: int
    timezone: str
    status: str
    created_at: datetime
    updated_at: datetime
    recipient_counts: EmailCampaignRecipientCounts


class EmailCampaignPreviewResponse(BaseModel):
    eligible_contact_count: int
    skipped_companies_without_email: int
    skipped_suppressed_contacts: int = 0
    skipped_company_ids: list[UUID]
    daily_capacity_per_day: int
    estimated_completion_at: datetime | None
