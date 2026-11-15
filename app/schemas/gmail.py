from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class GmailAccountAssigneeResponse(BaseModel):
    assignment_id: UUID
    user_id: UUID
    email: str
    assigned_at: datetime


class ConnectedGmailAccountResponse(BaseModel):
    id: UUID
    email_address: str
    google_account_id: str
    token_updated_at: datetime
    assigned_users: list[GmailAccountAssigneeResponse]

    model_config = ConfigDict(from_attributes=True)


class AssignedGmailAccountResponse(BaseModel):
    id: UUID
    email_address: str

    model_config = ConfigDict(from_attributes=True)


class UserGmailAssignmentCreate(BaseModel):
    user_id: UUID


class UserGmailAssignmentResponse(BaseModel):
    id: UUID
    org_id: UUID
    user_id: UUID
    gmail_account_id: UUID
    assigned_by: UUID
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class GmailMessageSummary(BaseModel):
    id: str
    thread_id: str
    from_address: str
    subject: str
    date: str
    snippet: str
    is_unread: bool


class GmailMessagesResponse(BaseModel):
    messages: list[GmailMessageSummary]
    next_page_token: str | None
    result_size_estimate: int


class GmailMessageDetailResponse(GmailMessageSummary):
    body_text: str


class GmailReplyInput(BaseModel):
    body_text: str = Field(min_length=1, max_length=20_000)

    @field_validator("body_text")
    @classmethod
    def require_nonblank_body(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Reply body cannot be blank.")
        return normalized


class GmailReplyResponse(BaseModel):
    id: str
    thread_id: str
