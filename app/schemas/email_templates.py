from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


class EmailTemplateCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    subject: str = Field(min_length=1, max_length=998)
    body_content: str = Field(min_length=1, max_length=200_000)

    @field_validator("name", "subject")
    @classmethod
    def trim_required_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("This field cannot be blank.")
        return normalized

    @field_validator("body_content")
    @classmethod
    def require_body_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("body_content cannot be blank.")
        return value


class EmailTemplateDraftRequest(BaseModel):
    prompt: str = Field(min_length=8, max_length=4_000)
    provider: Literal["gemini", "groq"] = "gemini"
    model: str | None = Field(default=None, min_length=1, max_length=200)

    @field_validator("prompt")
    @classmethod
    def trim_prompt(cls, value: str) -> str:
        normalized = value.strip()
        if len(normalized) < 8:
            raise ValueError("Describe the email you want in at least 8 characters.")
        return normalized

    @field_validator("model")
    @classmethod
    def trim_model(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("model cannot be blank.")
        return normalized


class GroqCompletionUsage(BaseModel):
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    requests_remaining: int | None = Field(default=None, ge=0)
    requests_limit: int | None = Field(default=None, ge=0)
    tokens_remaining: int | None = Field(default=None, ge=0)
    tokens_limit: int | None = Field(default=None, ge=0)


class EmailTemplateDraftResponse(EmailTemplateCreate):
    pass


class GroqEmailTemplateDraftResponse(EmailTemplateDraftResponse):
    model: str | None = None
    usage: GroqCompletionUsage | None = None


class EmailTemplateUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    subject: str | None = Field(default=None, min_length=1, max_length=998)
    body_content: str | None = Field(default=None, min_length=1, max_length=200_000)

    @model_validator(mode="after")
    def require_at_least_one_field(self) -> "EmailTemplateUpdate":
        if not self.model_fields_set:
            raise ValueError("At least one template field must be provided.")
        for field in self.model_fields_set:
            if getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null.")
        return self

    @field_validator("name", "subject")
    @classmethod
    def trim_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("This field cannot be blank.")
        return normalized

    @field_validator("body_content")
    @classmethod
    def require_optional_body_content(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("body_content cannot be blank.")
        return value


class EmailTemplateResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    org_id: UUID
    name: str
    subject: str
    body_content: str
    created_at: datetime
    updated_at: datetime


class EmailTemplateVariableMetadata(BaseModel):
    path: str
    placeholder: str
    label: str
    description: str


class EmailTemplatePreviewRequest(BaseModel):
    company_id: UUID
    contact_id: UUID | None = None
    gmail_account_id: UUID | None = None
    custom_variables: dict[str, Any] = Field(default_factory=dict)

    @field_validator("custom_variables")
    @classmethod
    def validate_custom_variables(
        cls,
        value: dict[str, Any],
    ) -> dict[str, Any]:
        _validate_custom_variables(value)
        return value


class EmailTemplateSendRequest(BaseModel):
    company_id: UUID
    contact_id: UUID
    gmail_account_id: UUID
    custom_variables: dict[str, Any] = Field(default_factory=dict)

    @field_validator("custom_variables")
    @classmethod
    def validate_custom_variables(
        cls,
        value: dict[str, Any],
    ) -> dict[str, Any]:
        _validate_custom_variables(value)
        return value


class EmailTemplatePreviewResponse(BaseModel):
    subject: str
    body_content: str


class EmailTemplateSendResponse(BaseModel):
    status: str
    message_id: str
    thread_id: str
    recipient_email: str


def _validate_custom_variables(value: dict[str, Any]) -> None:
    if len(value) > 100:
        raise ValueError("At most 100 custom variables may be provided.")

    def validate_value(item: Any, depth: int) -> None:
        if depth > 8:
            raise ValueError("Custom variables may not be nested more than 8 levels.")
        if isinstance(item, dict):
            if len(item) > 100:
                raise ValueError("A custom variable object may have at most 100 keys.")
            for key, nested_value in item.items():
                if (
                    not isinstance(key, str)
                    or not key
                    or len(key) > 100
                    or key.startswith("_")
                ):
                    raise ValueError("Custom variable keys must be public names.")
                validate_value(nested_value, depth + 1)
        elif isinstance(item, list):
            if len(item) > 100:
                raise ValueError("A custom variable list may contain at most 100 items.")
            for nested_value in item:
                validate_value(nested_value, depth + 1)
        elif isinstance(item, str) and len(item) > 20_000:
            raise ValueError("Custom variable text cannot exceed 20,000 characters.")
        elif item is not None and not isinstance(item, (str, int, float, bool)):
            raise ValueError("Custom variables must contain JSON-compatible values.")

    validate_value(value, 0)
