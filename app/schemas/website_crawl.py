from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class WebsiteCrawlRequest(BaseModel):
    website_url: HttpUrl


class WebsiteCrawlResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    website_url: str
    about_page_url: str | None
    about_content: str | None
    contact_page_url: str | None
    contact_content: str | None
    goals_page_url: str | None
    goals_content: str | None
    emails: list[str] = Field(default_factory=list)
    social_links: list[str] = Field(default_factory=list)
    crawled_at: datetime
