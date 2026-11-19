from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.pagespeed import PageSpeedScanSummary
from app.schemas.website_crawl import (
    CrawlPageClassification,
    WebsiteCrawlResponse,
)


class WebsiteHypeStatCountryDomains(BaseModel):
    country: str
    domains: int


class WebsiteResponse(BaseModel):
    id: UUID
    website_url: str
    domain: str
    favicon_url: str | None
    crawl_id: UUID | None
    is_crawled: bool
    crawl_job_status: Literal["queued", "running", "succeeded", "failed"] | None
    category: str | None
    page_type: str | None
    site_topic: list[str]
    language: str | None
    status: Literal["new", "lead", "checked"]
    discovery_source: str
    discovery_provider: str | None
    created_at: datetime
    created_by_email: str | None = None
    crawled_at: datetime | None
    crawl_signals: list[CrawlPageClassification] = Field(default_factory=list)
    hypestat_monthly_visits: int | None = None
    hypestat_referring_domains: int | None = None
    hypestat_global_rank: int | None = None
    hypestat_country: str | None = None
    hypestat_referring_domains_by_country: list[
        WebsiteHypeStatCountryDomains
    ] = Field(default_factory=list)
    hypestat_audience_countries: list[str] = Field(default_factory=list)


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


class WebsiteDiscoveryKeywordResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    keyword: str
    searched_at: datetime


class WebsiteHypeStatProfileResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    status: Literal[
        "pending",
        "succeeded",
        "queued",
        "failed",
        "invalid_domain",
        "removed",
    ]
    payload: dict[str, Any] | None
    error: str | None
    checked_at: datetime | None
    fetched_at: datetime | None


class WebsiteDetailResponse(BaseModel):
    website: WebsiteResponse
    crawl: WebsiteCrawlResponse | None
    pagespeed_scans: list[PageSpeedScanSummary] = Field(default_factory=list)
    hypestat_profile: WebsiteHypeStatProfileResponse | None = None
    crawl_jobs: list[WebsiteCrawlJobHistoryResponse] = Field(
        default_factory=list,
    )
    discovery_keywords: list[WebsiteDiscoveryKeywordResponse] = Field(
        default_factory=list,
    )


class WebsiteCorpusWordStat(BaseModel):
    word: str
    score: float = Field(ge=0)
    is_phrase: bool


class CrawlerWebsiteCorpusKeywords(BaseModel):
    domain: str
    words: list[WebsiteCorpusWordStat]


class CrawlerCorpusWordsResponse(BaseModel):
    sites: list[CrawlerWebsiteCorpusKeywords]


class WebsiteCorpusAnalysisResponse(BaseModel):
    website_id: UUID
    domain: str
    words: list[WebsiteCorpusWordStat] = Field(default_factory=list)
    computed_at: datetime | None


class WebsiteCorpusWordsResponse(BaseModel):
    websites: list[WebsiteCorpusAnalysisResponse] = Field(default_factory=list)


class WebsiteReclassificationBatchResponse(BaseModel):
    eligible_count: int
    recomputed_count: int
    skipped_count: int


class WebsiteAddToLeadsRequest(BaseModel):
    website_ids: list[UUID] = Field(min_length=1)


class WebsiteAddToLeadsResponse(BaseModel):
    requested_count: int
    created_count: int
    already_existing_count: int
    status_updated_count: int
    skipped_listing_count: int
    skipped_uncrawled_count: int
    not_found_count: int


class WebsiteBatchStatusUpdateRequest(BaseModel):
    website_ids: list[UUID] = Field(min_length=1, max_length=500)
    status: Literal["new", "lead", "checked"]


class WebsiteBatchStatusUpdateResponse(BaseModel):
    requested_count: int
    updated_count: int
    not_found_count: int


class WebsiteUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["new", "lead", "checked"] = "new"
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
