
import re
from datetime import date, datetime
from enum import Enum
from typing import Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    Field,
    HttpUrl,
    ValidationInfo,
    field_validator,
    model_validator,
)

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


MAX_DISCOVERY_KEYWORDS = 5


def parse_discovery_keywords(value: str) -> list[str]:
    keywords = [
        keyword.strip()
        for keyword in re.split(r"[,\r\n]+", value)
        if keyword.strip()
    ]
    normalized_keywords = []
    seen = set()
    for keyword in keywords:
        normalized = keyword.casefold()
        if normalized not in seen:
            normalized_keywords.append(keyword)
            seen.add(normalized)
    if not normalized_keywords:
        raise ValueError("Enter at least one keyword.")
    if any(len(keyword) < 3 for keyword in normalized_keywords):
        raise ValueError("Each keyword must contain at least 3 characters.")
    if len(normalized_keywords) > MAX_DISCOVERY_KEYWORDS:
        raise ValueError(
            f"Enter no more than {MAX_DISCOVERY_KEYWORDS} keywords."
        )
    return normalized_keywords


class DiscoverySearchRequest(BaseModel):
    query: str = Field(min_length=3, max_length=300)
    max_results: int = Field(default=20, ge=1, le=20)

    @field_validator("query")
    @classmethod
    def normalize_query(cls, value: str) -> str:
        return "\n".join(parse_discovery_keywords(value))


TavilySearchDepth = Literal["basic", "fast", "ultra-fast", "advanced"]
TavilyTopic = Literal["general", "news", "finance"]
TavilyTimeRange = Literal["day", "week", "month", "year"]


_DOMAIN_PATTERN = re.compile(
    r"^(?:\*\.)?(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$"
)


class TavilyDiscoverySearchRequest(DiscoverySearchRequest):
    search_depth: TavilySearchDepth = "basic"
    topic: TavilyTopic = "general"
    chunks_per_source: int = Field(default=3, ge=1, le=3)
    country: str | None = Field(default=None, min_length=1, max_length=100)
    time_range: TavilyTimeRange | None = None
    start_date: date | None = None
    end_date: date | None = None
    filter_by_published_date: bool = False
    include_domains: list[str] | None = Field(default=None, max_length=300)
    include_domains_mode: Literal["restrict", "prefer"] | None = None
    exclude_domains: list[str] | None = Field(default=None, max_length=150)
    include_answer: bool | Literal["basic", "advanced"] = False
    include_favicon: bool = True
    auto_parameters: bool = False
    min_score: float | None = Field(default=None, ge=0.0, le=1.0)
    providers: list[DiscoveryProvider] | None = Field(
        default=None,
        min_length=1,
        max_length=2,
    )

    @field_validator("country")
    @classmethod
    def country_must_be_lowercase(cls, value: str | None) -> str | None:
        if value is not None:
            normalized = value.strip()
            if not normalized or normalized != normalized.casefold():
                raise ValueError("Country must be a lowercase country name.")
            if not re.fullmatch(r"[a-z]+(?:[ -][a-z]+)*", normalized):
                raise ValueError("Country must be a lowercase country name.")
            return normalized
        return value

    @field_validator("start_date", "end_date", mode="before")
    @classmethod
    def dates_must_use_iso_format(cls, value: object) -> object:
        if value is None:
            return None
        if not isinstance(value, str) or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}",
            value,
        ):
            raise ValueError("Dates must use YYYY-MM-DD format.")
        return value

    @field_validator("include_domains", "exclude_domains")
    @classmethod
    def domains_must_be_valid(
        cls,
        value: list[str] | None,
        info: ValidationInfo,
    ) -> list[str] | None:
        if value is None:
            return None
        allow_wildcards = info.field_name == "include_domains"
        normalized_domains: list[str] = []
        for domain in value:
            normalized = domain.strip().casefold()
            if not _DOMAIN_PATTERN.fullmatch(normalized):
                raise ValueError(
                    "Domains must be hostnames"
                    + (" or wildcard hostnames." if allow_wildcards else ".")
                )
            if not allow_wildcards and normalized.startswith("*."):
                raise ValueError("Excluded domains cannot contain wildcards.")
            if normalized not in normalized_domains:
                normalized_domains.append(normalized)
        return normalized_domains

    @field_validator("providers")
    @classmethod
    def providers_must_be_unique(
        cls,
        value: list[DiscoveryProvider] | None,
    ) -> list[DiscoveryProvider] | None:
        if value is not None and len(value) != len(set(value)):
            raise ValueError("Search providers must be unique.")
        return value

    @model_validator(mode="after")
    def validate_date_filters(self) -> "TavilyDiscoverySearchRequest":
        if self.time_range is not None and (
            self.start_date is not None or self.end_date is not None
        ):
            raise ValueError(
                "Choose a time_range or custom start_date/end_date, not both."
            )
        if (
            self.start_date is not None
            and self.end_date is not None
            and self.start_date > self.end_date
        ):
            raise ValueError("start_date cannot be after end_date.")
        return self

    @model_validator(mode="after")
    def validate_keyword_result_limit(self) -> "TavilyDiscoverySearchRequest":
        if self.max_results < len(parse_discovery_keywords(self.query)):
            raise ValueError(
                "Set the result limit to at least one result per keyword."
            )
        return self


class DiscoveryAiSearchRequest(TavilyDiscoverySearchRequest):
    ai_provider: Literal["gemini", "groq"] = "gemini"
    model: str | None = Field(default=None, min_length=1, max_length=200)

    @field_validator("model")
    @classmethod
    def trim_model(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("model cannot be blank.")
        return normalized


class DiscoveryResult(BaseModel):
    title: str
    url: HttpUrl
    content: str
    score: float
    provider: DiscoveryProvider = DiscoveryProvider.TAVILY
    favicon: HttpUrl | None = None
    published_date: str | None = None


class DiscoveryDomainRatingRequest(BaseModel):
    domains: list[str] = Field(min_length=1, max_length=20)

    @field_validator("domains")
    @classmethod
    def domains_must_be_valid(
        cls,
        value: list[str],
    ) -> list[str]:
        normalized_domains = []
        for domain in value:
            normalized = domain.strip().casefold()
            if normalized.startswith("www."):
                normalized = normalized[4:]
            if not _DOMAIN_PATTERN.fullmatch(normalized):
                raise ValueError("Domains must be valid hostnames.")
            if normalized not in normalized_domains:
                normalized_domains.append(normalized)
        return normalized_domains


class DiscoveryDomainRatingResult(BaseModel):
    domain: str
    status: Literal["succeeded", "failed"]
    domain_rating: float | None = None
    license_url: str | None = None
    fetched_at: datetime | None = None
    error: str | None = None


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
    search_queries: list[str] = Field(default_factory=list, max_length=5)
    search_id: UUID | None = None
    answer: str | None = None
    credits_used: float | None = None
    provider_jobs: list[DiscoveryProviderJobResponse] = Field(
        default_factory=list,
    )
    provider_failures: list[DiscoveryProviderFailure] = Field(
        default_factory=list,
    )


class DiscoveryAiSearchResponse(DiscoverySearchResponse):
    search_queries: list[str] = Field(min_length=1, max_length=7)


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
    max_results: int = Field(default=20, ge=1, le=100)
    results: list[DiscoveryWebsiteInput] = Field(max_length=100)
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
    hypestat_profiles_requested: int = 0
    hypestat_profiles_reused: int = 0
    hypestat_profiles_succeeded: int = 0
    hypestat_profiles_queued: int = 0
    hypestat_profiles_failed: int = 0


class DiscoveryIngestResponse(BaseModel):
    query: str
    results_found: int
    candidates_created: int
    results_skipped: int
    websites_crawled: int
    companies_enriched: int
    contacts_added: int
    crawl_failures: list[DiscoveryCrawlFailure] = Field(default_factory=list)
    pagespeed_scans_queued: int
    pagespeed_jobs: list[PageSpeedScanJobResponse] = Field(default_factory=list)