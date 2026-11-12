from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict


PageSpeedStrategy = Literal["mobile", "desktop"]


class PageSpeedScanRequest(BaseModel):
    strategy: PageSpeedStrategy


class PageSpeedScanSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    website_url: str
    strategy: PageSpeedStrategy
    category_scores: dict[str, int | None]
    scanned_at: datetime


class PageSpeedScanResponse(PageSpeedScanSummary):
    report: dict[str, Any]


class PageSpeedScanJobResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    company_ids: list[UUID]
    website_url: str
    strategy: PageSpeedStrategy
    status: Literal["queued", "running", "succeeded", "failed"]
    attempts: int
    error: str | None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
