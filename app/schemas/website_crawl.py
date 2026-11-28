from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator


class PageClassificationSignals(BaseModel):
    model_config = ConfigDict(extra="ignore")

    page_title: str | None = None
    meta_description: str | None = None
    og_title: str | None = None
    og_description: str | None = None
    og_type: str | None = None
    generator: str | None = None
    json_ld_scripts: list[str] = Field(default_factory=list)
    json_ld_schema_types: list[str] = Field(default_factory=list)
    microdata_schema_types: list[str] = Field(default_factory=list)
    heading_texts: list[str] = Field(default_factory=list)
    first_paragraph_text: str | None = None
    meta_classification_terms: list[str] = Field(default_factory=list)
    url_classification_terms: list[str] = Field(default_factory=list)
    body_classification_terms: list[str] = Field(default_factory=list)
    article_count: int = 0
    password_input_count: int = 0
    shopping_elements: dict[str, int] = Field(default_factory=dict)
    nav_link_texts: list[str] = Field(default_factory=list)
    footer_link_texts: list[str] = Field(default_factory=list)
    header_link_texts: list[str] = Field(default_factory=list)
    cta_texts: list[str] = Field(default_factory=list)
    url_path_patterns: list[str] = Field(default_factory=list)


class CrawlPageClassification(BaseModel):
    model_config = ConfigDict(extra="ignore")

    url: str
    status_code: int | None = None
    error: str | None = None
    rendering_mode: str | None = None
    classification_signals: PageClassificationSignals | None = None

    @field_validator("status_code")
    @classmethod
    def omit_missing_http_status(cls, value: int | None) -> int | None:
        return None if value == 0 else value


class WebsiteCrawlRequest(BaseModel):
    website_url: HttpUrl


class ManualWebsiteCrawlCompletionRequest(BaseModel):
    site_type: str = Field(min_length=1, max_length=64)
    website_topics: list[str] = Field(default_factory=list, max_length=30)
    language: str | None = Field(default=None, max_length=35)
    home_page_url: HttpUrl | None = None
    home_content: str | None = Field(default=None, max_length=20000)
    about_page_url: HttpUrl | None = None
    about_content: str | None = Field(default=None, max_length=20000)
    blog_page_url: HttpUrl | None = None
    blog_content: str | None = Field(default=None, max_length=20000)
    service_page_url: HttpUrl | None = None
    service_content: str | None = Field(default=None, max_length=20000)
    contact_page_url: HttpUrl | None = None
    contact_content: str | None = Field(default=None, max_length=20000)
    goals_page_url: HttpUrl | None = None
    goals_content: str | None = Field(default=None, max_length=20000)
    emails: list[str] = Field(default_factory=list, max_length=100)
    phones: list[str] = Field(default_factory=list, max_length=100)
    social_links: list[HttpUrl] = Field(default_factory=list, max_length=100)

    @field_validator("site_type")
    @classmethod
    def site_type_must_not_be_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Site type is required.")
        return normalized

    @field_validator("website_topics", "emails", "phones")
    @classmethod
    def normalize_manual_values(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(value.strip() for value in values if value.strip()))


class WebsiteCrawlBatchRequest(BaseModel):
    website_urls: list[HttpUrl] = Field(min_length=1, max_length=100)


class WebsiteCrawlResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    website_url: str
    home_page_url: str | None
    home_content: str | None
    about_page_url: str | None
    about_content: str | None
    blog_page_url: str | None
    blog_content: str | None
    service_page_url: str | None
    service_content: str | None
    contact_page_url: str | None
    contact_content: str | None
    goals_page_url: str | None
    goals_content: str | None
    classification_signals: list[CrawlPageClassification] = Field(
        default_factory=list,
    )
    social_links: list[str] = Field(default_factory=list)
    source: Literal["crawler", "manual"] = "crawler"
    crawled_at: datetime
    site_type: str | None = None


class WebsiteCrawlJobResponse(BaseModel):
    id: UUID
    website_url: str
    status: Literal["queued", "running", "succeeded", "failed", "manual"]
    attempts: int
    error: str | None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    result: WebsiteCrawlResponse | None


ManualEnrichmentStatus = Literal[
    "queued",
    "already_available",
    "already_queued",
    "not_configured",
    "disabled",
]


class ManualWebsiteCrawlCompletionResponse(BaseModel):
    job_id: UUID
    website_id: UUID
    crawl_id: UUID
    status: Literal["manual"]
    ahrefs_domain_rating: ManualEnrichmentStatus
    hypestat: ManualEnrichmentStatus
    pagespeed: ManualEnrichmentStatus


class WebsiteCrawlBatchJobResponse(BaseModel):
    id: UUID
    website_url: str
    status: Literal["queued"]


class WebsiteCrawlBatchExcludedResponse(BaseModel):
    website_url: str
    reason: str


class WebsiteCrawlBatchResponse(BaseModel):
    jobs: list[WebsiteCrawlBatchJobResponse]
    excluded: list[WebsiteCrawlBatchExcludedResponse] = Field(
        default_factory=list,
    )
