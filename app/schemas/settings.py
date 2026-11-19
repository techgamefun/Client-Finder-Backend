from datetime import datetime
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.discovery import DiscoveryProvider, TavilySearchDepth


_DOMAIN_PATTERN = re.compile(
    r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$"
)


class OctoparseTemplateConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    template_name: str = Field(min_length=1, max_length=255)
    query_field: str = Field(min_length=1, max_length=255)
    additional_parameters: dict[str, Any] = Field(default_factory=dict)
    title_field: str = Field(min_length=1, max_length=255)
    url_field: str = Field(min_length=1, max_length=255)
    content_field: str | None = Field(default=None, max_length=255)
    score_field: str | None = Field(default=None, max_length=255)


class OctoparseTemplateOption(BaseModel):
    template_name: str
    slug: str | None = None
    execution_mode: str | None = None
    description: str | None = None
    input_schema: list[dict[str, Any]] = Field(default_factory=list)
    output_schema: list[dict[str, Any]] = Field(default_factory=list)
    source_tree: Any = None
    source_summary: Any = None


class OctoparseTemplateSearchResponse(BaseModel):
    recommended_template_name: str | None = None
    templates: list[OctoparseTemplateOption]


class ApplicationSettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    crawl_max_pages: int | None = Field(default=None, ge=1, le=6)
    crawl_max_depth: int | None = Field(default=None, ge=0, le=2)
    tavily_api_key: str | None = Field(default=None, max_length=512)
    octoparse_api_key: str | None = Field(default=None, max_length=512)
    hypestat_api_key: str | None = Field(default=None, max_length=512)
    pagespeed_api_key: str | None = Field(default=None, max_length=512)
    gemini_api_key: str | None = Field(default=None, max_length=512)
    hypestat_auto_fetch_enabled: bool | None = None
    tavily_default_search_depth: TavilySearchDepth | None = None
    tavily_default_country: str | None = Field(
        default=None,
        min_length=1,
        max_length=100,
    )
    tavily_default_exclude_domains: list[str] | None = Field(
        default=None,
        max_length=150,
    )
    selected_discovery_tools: list[DiscoveryProvider] | None = Field(
        default=None,
        min_length=1,
        max_length=2,
    )
    octoparse_template: OctoparseTemplateConfiguration | None = None

    @field_validator("selected_discovery_tools")
    @classmethod
    def selected_tools_must_be_unique(
        cls,
        value: list[DiscoveryProvider] | None,
    ) -> list[DiscoveryProvider] | None:
        if value is not None and len(value) != len(set(value)):
            raise ValueError("Selected discovery tools must be unique.")
        return value

    @field_validator("tavily_default_country")
    @classmethod
    def country_must_be_lowercase(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized or normalized != normalized.casefold():
            raise ValueError("Country must be a lowercase country name.")
        if not re.fullmatch(r"[a-z]+(?:[ -][a-z]+)*", normalized):
            raise ValueError("Country must be a lowercase country name.")
        return normalized

    @field_validator("tavily_default_exclude_domains")
    @classmethod
    def excluded_domains_must_be_valid(
        cls,
        value: list[str] | None,
    ) -> list[str] | None:
        if value is None:
            return None
        normalized_domains: list[str] = []
        for domain in value:
            normalized = domain.strip().casefold()
            if not _DOMAIN_PATTERN.fullmatch(normalized):
                raise ValueError("Excluded domains must be hostnames.")
            if normalized not in normalized_domains:
                normalized_domains.append(normalized)
        return normalized_domains


class ApplicationSettingsResponse(BaseModel):
    crawl_max_pages: int
    crawl_max_depth: int
    tavily_configured: bool
    tavily_key_source: str
    octoparse_configured: bool
    octoparse_key_source: str
    hypestat_configured: bool
    hypestat_auto_fetch_enabled: bool
    hypestat_service_auth_configured: bool
    pagespeed_configured: bool
    pagespeed_key_source: str
    gemini_configured: bool
    selected_discovery_tools: list[DiscoveryProvider]
    tavily_default_search_depth: TavilySearchDepth = "basic"
    tavily_default_country: str | None = None
    tavily_default_exclude_domains: list[str] = Field(default_factory=list)
    octoparse_template: OctoparseTemplateConfiguration | None = None


class TavilyKeyUsage(BaseModel):
    usage: int
    limit: int | None = None
    search_usage: int | None = None
    extract_usage: int | None = None
    crawl_usage: int | None = None
    map_usage: int | None = None
    research_usage: int | None = None


class TavilyAccountUsage(BaseModel):
    current_plan: str | None = None
    plan_usage: int | None = None
    plan_limit: int | None = None
    paygo_usage: int | None = None
    paygo_limit: int | None = None
    search_usage: int | None = None
    extract_usage: int | None = None
    crawl_usage: int | None = None
    map_usage: int | None = None
    research_usage: int | None = None


class TavilyUsageResponse(BaseModel):
    key: TavilyKeyUsage
    account: TavilyAccountUsage | None = None


class OctoparseUsageResponse(BaseModel):
    period_start: datetime
    tasks_started: int
    tasks_running: int
    tasks_succeeded: int
    tasks_failed: int
    all_time_tasks_started: int
