from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.website_crawl import (
    CrawlPageClassification,
    WebsiteCrawlResponse,
)


class WebsiteResponse(BaseModel):
    id: UUID
    website_url: str
    domain: str
    crawl_id: UUID | None
    is_crawled: bool
    crawl_job_status: Literal["queued", "running", "succeeded", "failed"] | None
    category: str | None
    page_type: str | None
    site_topic: list[str]
    language: str | None
    status: Literal["new"]
    site_type: Literal["normal", "listing"]
    discovery_source: str
    discovery_provider: str | None
    created_at: datetime
    crawled_at: datetime | None
    crawl_signals: list[CrawlPageClassification] = Field(default_factory=list)


class WebsiteCrawlJobHistoryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    status: Literal["queued", "running", "succeeded", "failed"]
    attempts: int
    error: str | None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


class WebsiteDetailResponse(BaseModel):
    website: WebsiteResponse
    crawl: WebsiteCrawlResponse | None
    crawl_jobs: list[WebsiteCrawlJobHistoryResponse] = Field(
        default_factory=list,
    )


class WebsiteUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page_type: Literal[
        "business_site",
        "business_listing",
        "article",
        "blog",
        "news",
        "ecommerce",
        "social_media",
        "cms",
        "software_tool",
        "documentation",
        "forum",
        "media",
        "other",
    ] | None = None
    site_topic: list[str] | None = Field(default=None, max_length=20)
    language: str | None = Field(default=None, max_length=35)

    @field_validator("site_topic")
    @classmethod
    def normalize_site_topic(
        cls,
        topics: list[str] | None,
    ) -> list[str] | None:
        if topics is None:
            return None
        return list(
            dict.fromkeys(
                topic.strip()[:100]
                for topic in topics
                if topic.strip()
            )
        )

    @field_validator("language")
    @classmethod
    def normalize_language(cls, language: str | None) -> str | None:
        if language is None:
            return None
        language = language.strip()
        return language or None
