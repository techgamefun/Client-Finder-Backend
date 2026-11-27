from datetime import date
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

from app.schemas.discovery import (
    DiscoveryProvider,
    TavilyDiscoverySearchRequest,
    TavilySearchDepth,
    TavilyTimeRange,
    TavilyTopic,
)


ThemePreference = Literal["light", "dark", "system"]
WebsiteColumn = Literal[
    "keywords",
    "website_type",
    "site_topic",
    "hypestat_monthly_visits",
    "hypestat_referring_domains",
    "hypestat_global_rank",
    "hypestat_country",
    "hypestat_backlink_countries",
    "hypestat_audience_countries",
    "language",
    "crawled_at",
    "ahrefs_domain_rating",
    "pagespeed_desktop_performance",
    "pagespeed_desktop_accessibility",
    "pagespeed_desktop_best_practices",
    "pagespeed_desktop_seo",
    "pagespeed_mobile_performance",
    "pagespeed_mobile_accessibility",
    "pagespeed_mobile_best_practices",
    "pagespeed_mobile_seo",
]


class TavilySearchPreferences(BaseModel):
    model_config = ConfigDict(extra="forbid")

    topic: TavilyTopic | None = None
    search_depth: TavilySearchDepth | None = None
    max_results: Literal[5, 10, 15, 20] | None = None
    country: str | None = Field(default=None, max_length=100)
    include_domains: list[str] | None = Field(default=None, max_length=300)
    include_domains_mode: Literal["restrict", "prefer"] | None = None
    exclude_domains: list[str] | None = Field(default=None, max_length=150)
    date_mode: Literal["relative", "custom"] | None = None
    time_range: TavilyTimeRange | None = None
    start_date: date | None = None
    end_date: date | None = None
    filter_by_published_date: bool | None = None
    min_score: float | None = Field(default=None, ge=0.0, le=1.0)
    include_answer: bool | None = None
    auto_parameters: bool | None = None
    auto_find_more: bool | None = None

    @field_validator("country")
    @classmethod
    def country_must_match_search_contract(cls, value: str | None) -> str | None:
        if value is None or value == "":
            return value
        validated = TavilyDiscoverySearchRequest.model_validate(
            {"query": "saved preferences", "country": value}
        )
        return validated.country

    @field_validator("include_domains", "exclude_domains")
    @classmethod
    def domains_must_match_search_contract(
        cls,
        value: list[str] | None,
        info: ValidationInfo,
    ) -> list[str] | None:
        if value is None:
            return value
        validated = TavilyDiscoverySearchRequest.model_validate(
            {"query": "saved preferences", info.field_name: value}
        )
        return getattr(validated, info.field_name)

    @model_validator(mode="after")
    def custom_dates_must_be_ordered(self) -> "TavilySearchPreferences":
        if (
            self.start_date is not None
            and self.end_date is not None
            and self.start_date > self.end_date
        ):
            raise ValueError("start_date cannot be after end_date.")
        return self


class UserSearchPreferences(BaseModel):
    model_config = ConfigDict(extra="forbid")

    providers: list[DiscoveryProvider] | None = Field(
        default=None,
        min_length=1,
        max_length=2,
    )
    tavily: TavilySearchPreferences | None = None

    @field_validator("providers")
    @classmethod
    def providers_must_be_unique(
        cls,
        value: list[DiscoveryProvider] | None,
    ) -> list[DiscoveryProvider] | None:
        if value is not None and len(value) != len(set(value)):
            raise ValueError("Search providers must be unique.")
        return value


class WebsiteTablePreferences(BaseModel):
    model_config = ConfigDict(extra="forbid")

    visible_columns: list[WebsiteColumn] | None = Field(
        default=None,
        max_length=20,
    )

    @field_validator("visible_columns")
    @classmethod
    def columns_must_be_unique(
        cls,
        value: list[WebsiteColumn] | None,
    ) -> list[WebsiteColumn] | None:
        if value is not None and len(value) != len(set(value)):
            raise ValueError("Website columns must be unique.")
        return value


class UserPreferencesUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    theme: ThemePreference | None = None
    sidebar_collapsed: bool | None = None
    search: UserSearchPreferences | None = None
    websites: WebsiteTablePreferences | None = None

    @model_validator(mode="after")
    def require_a_preference(self) -> "UserPreferencesUpdate":
        if not self.model_fields_set:
            raise ValueError("At least one user preference must be provided.")
        for field in ("theme", "sidebar_collapsed", "search", "websites"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null.")
        return self


class UserPreferencesResponse(BaseModel):
    theme: ThemePreference = "light"
    sidebar_collapsed: bool = False
    search: UserSearchPreferences = Field(default_factory=UserSearchPreferences)
    websites: WebsiteTablePreferences = Field(
        default_factory=WebsiteTablePreferences,
    )
