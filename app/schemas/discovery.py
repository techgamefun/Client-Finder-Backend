
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field, HttpUrl


class DiscoverySearchRequest(BaseModel):
    query: str = Field(min_length=3, max_length=300)
    max_results: int = Field(default=5, ge=1, le=10)


class DiscoveryResult(BaseModel):
    title: str
    url: HttpUrl
    content: str
    score: float


class DiscoverySearchResponse(BaseModel):
    query: str
    results: list[DiscoveryResult]
    search_id: UUID | None = None


class SearchKeywordResponse(BaseModel):
    id: UUID
    keyword: str
    searched_at: datetime


class DiscoveryIngestRequest(DiscoverySearchRequest):
    results: list[DiscoveryResult] | None = Field(default=None, max_length=10)
    search_id: UUID | None = None


class DiscoveryCrawlFailure(BaseModel):
    source_url: str
    detail: str


class DiscoveryPageSpeedFailure(BaseModel):
    website_url: str
    strategy: str
    detail: str


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
    pagespeed_scans_saved: int
    pagespeed_failures: list[DiscoveryPageSpeedFailure] = Field(
        default_factory=list
    )
