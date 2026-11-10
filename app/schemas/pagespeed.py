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
