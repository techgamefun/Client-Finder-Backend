from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


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
    rendering_mode: str | None = None
    classification_signals: PageClassificationSignals | None = None


class WebsiteCrawlRequest(BaseModel):
    website_url: HttpUrl


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
    emails: list[str] = Field(default_factory=list)
    social_links: list[str] = Field(default_factory=list)
    crawled_at: datetime
    site_type: str | None = None


class WebsiteCrawlJobResponse(BaseModel):
    id: UUID
    website_url: str
    status: Literal["queued", "running", "succeeded", "failed"]
    attempts: int
    error: str | None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    result: WebsiteCrawlResponse | None
