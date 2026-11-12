from pydantic import BaseModel, ConfigDict, Field


class ApplicationSettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    crawl_max_pages: int = Field(ge=1, le=6)
    crawl_max_depth: int = Field(ge=0, le=2)
    tavily_api_key: str | None = Field(default=None, max_length=512)


class ApplicationSettingsResponse(BaseModel):
    crawl_max_pages: int
    crawl_max_depth: int
    tavily_configured: bool
    tavily_key_source: str


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
