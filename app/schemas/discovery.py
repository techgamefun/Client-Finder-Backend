
from datetime import datetime
from enum import Enum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, HttpUrl

from app.schemas.pagespeed import PageSpeedScanJobResponse


class DiscoveryToolCategory(str, Enum):
    SEARCH_ENGINE = "search_engine"
    BUSINESS_LISTING_WEBSITE = "business_listing_website"


class DiscoveryProvider(str, Enum):
    TAVILY = "tavily"
    OCTOPARSE = "octoparse"


class DiscoveryTool(BaseModel):
    id: DiscoveryProvider
    category: DiscoveryToolCategory
    name: str


class DiscoverySearchRequest(BaseModel):
    query: str = Field(min_length=3, max_length=300)
    max_results: int = Field(default=20, ge=1, le=20)


class DiscoveryResult(BaseModel):
    title: str
    url: HttpUrl
    content: str
    score: float
    provider: DiscoveryProvider = DiscoveryProvider.TAVILY


class DiscoveryProviderFailure(BaseModel):
    provider: DiscoveryProvider
    detail: str


class DiscoveryProviderJobResponse(BaseModel):
    id: UUID
    provider: DiscoveryProvider
    status: Literal["running", "succeeded", "failed"]
    results: list[DiscoveryResult] = Field(default_factory=list)
    error: str | None = None
    warning: str | None = None


class DiscoverySearchResponse(BaseModel):
    query: str
    results: list[DiscoveryResult]
    search_id: UUID | None = None
    provider_jobs: list[DiscoveryProviderJobResponse] = Field(
        default_factory=list,
    )
    provider_failures: list[DiscoveryProviderFailure] = Field(
        default_factory=list,
    )


class SearchKeywordResponse(BaseModel):
    id: UUID
    keyword: str
    searched_at: datetime


class DiscoveryIngestRequest(DiscoverySearchRequest):
    results: list[DiscoveryResult] | None = Field(default=None, max_length=20)
    search_id: UUID | None = None


class DiscoveryWebsiteInput(BaseModel):
    url: HttpUrl
    provider: DiscoveryProvider


class DiscoveryWebsiteSaveRequest(DiscoverySearchRequest):
    results: list[DiscoveryWebsiteInput] = Field(max_length=20)
    search_id: UUID | None = None


class DiscoveryCrawlFailure(BaseModel):
    source_url: str
    detail: str


class DiscoveryWebsiteSaveResponse(BaseModel):
    query: str
    results_found: int
    websites_saved: int
    crawl_jobs_enqueued: int
    crawl_jobs_already_present: int
    domains_skipped: int


class DiscoveryIngestResponse(BaseModel):
    query: str
    results_found: int
    candidates_created: int
    sources_added: int
    results_skipped: int
    websites_crawled: int
    companies_enriched: int
    contacts_added: int
    crawl_failures: list[DiscoveryCrawlFailure] = Field(default_factory=list)
    pagespeed_scans_queued: int
    pagespeed_jobs: list[PageSpeedScanJobResponse] = Field(default_factory=list)