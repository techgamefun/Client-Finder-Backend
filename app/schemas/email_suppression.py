from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


SuppressionTarget = Literal["email", "domain"]
SuppressionReason = Literal[
    "unsubscribe",
    "hard_bounce",
    "complaint",
    "manual_dnc",
]


class UnsubscribeRequest(BaseModel):
    token: str = Field(min_length=1, max_length=16_384)
    email: str | None = Field(default=None, max_length=320)


class UnsubscribeResponse(BaseModel):
    valid: bool
    requires_email: bool = False


class SuppressionCreate(BaseModel):
    target_type: SuppressionTarget
    value: str = Field(min_length=1, max_length=320)
    reason: SuppressionReason


class SuppressionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    target_type: SuppressionTarget
    normalized_value: str
    reason: SuppressionReason
    global_dnc: bool
    created_at: datetime


class SuppressionListResponse(BaseModel):
    items: list[SuppressionResponse]
