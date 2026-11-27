from datetime import date, datetime
import re
from typing import Any, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    field_validator,
    model_validator,
)

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
    business_name: str | None = None
    industry: str | None = None
    city: str | None = None
    state: str | None = None
    software_need: str | None = None
    lead_score: int | None = None
    last_email_sent_at: datetime | None = None
    domain_resolution_needed: bool = False
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
    status: Literal[
        "new",
        "checked",
        "qualified",
        "contacted",
        "converted",
        "disqualified",
        "needs_review",
    ]
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


class WebsiteContactCreate(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    role: str | None = Field(default=None, max_length=150)
    business_email: str | None = Field(default=None, max_length=320)
    business_phone: str | None = Field(default=None, max_length=50)
    is_publicly_listed: bool = False

    @field_validator("name", "role", "business_email", "business_phone")
    @classmethod
    def trim_optional_fields(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None

    @field_validator("business_email")
    @classmethod
    def validate_business_email(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(
            r"[^@\s]+@[^@\s.]+(?:\.[^@\s.]+)+",
            value,
        ):
            raise ValueError("Enter a valid business email address.")
        return value

    @model_validator(mode="after")
    def require_contact_details(self) -> "WebsiteContactCreate":
        if not any(
            (self.name, self.role, self.business_email, self.business_phone)
        ):
            raise ValueError("Provide at least one contact detail.")
        return self


class WebsiteContactUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    role: str | None = Field(default=None, max_length=150)
    business_email: str | None = Field(default=None, max_length=320)
    business_phone: str | None = Field(default=None, max_length=50)
    is_publicly_listed: bool | None = None

    @field_validator("name", "role", "business_email", "business_phone")
    @classmethod
    def trim_optional_fields(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None

    @field_validator("business_email")
    @classmethod
    def validate_business_email(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(
            r"[^@\s]+@[^@\s.]+(?:\.[^@\s.]+)+",
            value,
        ):
            raise ValueError("Enter a valid business email address.")
        return value

    @model_validator(mode="after")
    def require_changes(self) -> "WebsiteContactUpdate":
        if not self.model_fields_set:
            raise ValueError("At least one contact field must be provided.")
        if "is_publicly_listed" in self.model_fields_set:
            if self.is_publicly_listed is None:
                raise ValueError("is_publicly_listed cannot be null.")
        return self


class WebsiteContactResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    website_id: UUID
    name: str | None
    role: str | None
    business_email: str | None
    business_phone: str | None
    is_publicly_listed: bool


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
    contacts: list["WebsiteContactResponse"] = Field(default_factory=list)
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


class WebsiteGroupCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    website_ids: list[UUID] = Field(default_factory=list, max_length=5000)

    @field_validator("name")
    @classmethod
    def trim_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Group name cannot be blank.")
        return normalized

    @field_validator("website_ids")
    @classmethod
    def unique_website_ids(cls, value: list[UUID]) -> list[UUID]:
        if len(set(value)) != len(value):
            raise ValueError("Website IDs must be unique.")
        return value


class WebsiteGroupUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=160)

    @field_validator("name")
    @classmethod
    def trim_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Group name cannot be blank.")
        return normalized


class WebsiteGroupMembershipUpdate(BaseModel):
    website_ids: list[UUID] = Field(min_length=1, max_length=5000)
    action: Literal["add", "move", "ungroup"]
    group_id: UUID | None = None

    @field_validator("website_ids")
    @classmethod
    def unique_website_ids(cls, value: list[UUID]) -> list[UUID]:
        if len(set(value)) != len(value):
            raise ValueError("Website IDs must be unique.")
        return value

    @model_validator(mode="after")
    def require_group_for_membership(self) -> "WebsiteGroupMembershipUpdate":
        if self.action in ("add", "move") and self.group_id is None:
            raise ValueError("Choose a group for this action.")
        return self


class WebsiteGroupResponse(BaseModel):
    id: UUID
    name: str
    created_at: datetime
    website_count: int


class WebsiteGroupMembershipResponse(BaseModel):
    updated_count: int


class WebsiteBatchStatusUpdateRequest(BaseModel):
    website_ids: list[UUID] = Field(min_length=1, max_length=500)
    status: Literal[
        "new",
        "checked",
        "qualified",
        "contacted",
        "converted",
        "disqualified",
        "needs_review",
    ]


class WebsiteBatchStatusUpdateResponse(BaseModel):
    requested_count: int
    updated_count: int
    not_found_count: int


class WebsiteUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal[
        "new",
        "checked",
        "qualified",
        "contacted",
        "converted",
        "disqualified",
        "needs_review",
    ] = "new"
    website_url: HttpUrl | None = None
    business_name: str | None = Field(default=None, max_length=200)
    industry: str | None = Field(default=None, max_length=150)
    city: str | None = Field(default=None, max_length=100)
    state: str | None = Field(default=None, max_length=100)
    software_need: str | None = None
    lead_score: int | None = Field(default=None, ge=0)
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
