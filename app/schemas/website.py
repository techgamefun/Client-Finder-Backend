from datetime import date, datetime
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
    crawl_job_status: Literal[
        "queued",
        "running",
        "succeeded",
        "failed",
        "skipped",
    ] | None
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
    ahrefs_domain_rating: float | None = None
    ahrefs_domain_rating_license_url: str | None = None
    ahrefs_domain_rating_fetched_at: datetime | None = None
    pagespeed_desktop: PageSpeedScanSummary | None = None
    pagespeed_mobile: PageSpeedScanSummary | None = None
    hypestat_referring_domains_by_country: list[
        WebsiteHypeStatCountryDomains
    ] = Field(default_factory=list)
    hypestat_audience_countries: list[str] = Field(default_factory=list)


class WebsiteRegistryPageResponse(BaseModel):
    items: list[WebsiteResponse]
    total_count: int
    indexed_count: int
    website_count: int


class WebsiteRegistryFacetsResponse(BaseModel):
    categories: list[str]
    discovery_sources: list[str]
    hypestat_countries: list[str]
    backlink_countries: list[str]
    audience_countries: list[str]


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


class WebsiteAhrefsMetricsResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    snapshot_date: date
    domain_rating: float
    domain_rating_license_url: str
    organic_traffic: int | None = None
    organic_traffic_cost_cents: int | None = None
    organic_keywords: int | None = None
    backlinks_live: int | None = None
    backlinks_all_time: int | None = None
    refdomains_live: int | None = None
    refdomains_all_time: int | None = None
    units_used: int | None = None
    fetched_at: datetime


class WebsiteDetailResponse(BaseModel):
    website: WebsiteResponse
    crawl: WebsiteCrawlResponse | None
    pagespeed_scans: list[PageSpeedScanSummary] = Field(default_factory=list)
    hypestat_profile: WebsiteHypeStatProfileResponse | None = None
    ahrefs_metrics: WebsiteAhrefsMetricsResponse | None = None
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
    pending_website_ids: list[UUID] = Field(default_factory=list)
    failed_website_ids: list[UUID] = Field(default_factory=list)


class WebsiteCorpusAnalysisJobResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    website_id: UUID
    website_url: str
    domain: str
    source_crawled_at: datetime
    status: Literal["queued", "running", "succeeded", "failed", "skipped"]
    attempts: int
    error: str | None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


class WebsiteHypeStatFetchJobResponse(BaseModel):
    id: UUID
    website_id: UUID
    website_url: str
    domain: str
    status: Literal["queued", "running", "succeeded", "failed", "skipped"]
    attempts: int
    error: str | None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


class WebsiteReclassificationBatchResponse(BaseModel):
    eligible_count: int
    recomputed_count: int
    skipped_count: int


class WebsiteBulkEnrichmentResponse(BaseModel):
    website_count: int
    queued_website_count: int
    queued_job_count: int
    missing_website_count: int
    in_progress_job_count: int
    skipped_website_count: int


class WebsiteEnrichmentCountsResponse(BaseModel):
    classification_missing_count: int
    ahrefs_missing_count: int
    hypestat_missing_count: int
    pagespeed_missing_scan_count: int


class WebsiteAddToLeadsRequest(BaseModel):
    website_ids: list[UUID] = Field(min_length=1, max_length=500)
    batch_id: UUID | None = None
    batch_name: str | None = Field(default=None, max_length=160)

    @field_validator("batch_name")
    @classmethod
    def trim_batch_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("Group name cannot be blank.")
        return normalized


class WebsiteAddToLeadsResponse(BaseModel):
    requested_count: int
    created_count: int
    already_existing_count: int
    status_updated_count: int
    skipped_listing_count: int
    skipped_uncrawled_count: int
    not_found_count: int
    batch_id: UUID | None = None
    batch_name: str | None = None


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
