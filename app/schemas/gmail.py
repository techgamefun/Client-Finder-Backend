from datetime import datetime
from email.headerregistry import Address
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


class GmailLabelResponse(BaseModel):
    id: str
    name: str
    type: str


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


class GmailComposeInput(BaseModel):
    to: list[str] = Field(min_length=1, max_length=50)
    cc: list[str] = Field(default_factory=list, max_length=50)
    bcc: list[str] = Field(default_factory=list, max_length=50)
    subject: str = Field(max_length=998)
    body_text: str = Field(min_length=1, max_length=20_000)

    @field_validator("to", "cc", "bcc")
    @classmethod
    def validate_recipients(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for value in values:
            address = value.strip()
            if (
                not address
                or any(character in address for character in "\r\n,;")
            ):
                raise ValueError("Enter valid email addresses for all recipients.")
            try:
                Address(addr_spec=address)
            except ValueError as exc:
                raise ValueError(
                    "Enter valid email addresses for all recipients."
                ) from exc
            normalized.append(address)
        return normalized

    @field_validator("subject")
    @classmethod
    def validate_subject(cls, value: str) -> str:
        normalized = value.strip()
        if "\r" in normalized or "\n" in normalized:
            raise ValueError("Subject cannot contain line breaks.")
        return normalized

    @field_validator("body_text")
    @classmethod
    def require_nonblank_body(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Message body cannot be blank.")
        return normalized
