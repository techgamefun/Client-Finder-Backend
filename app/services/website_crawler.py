from datetime import datetime, timezone

import httpx
from fastapi import HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.config import settings
from app.models import WebsiteCrawl
from app.schemas.website_crawl import CrawlPageClassification
from app.services.application_settings import get_crawl_settings
from app.services.public_url import _normalize_website_url
from app.services.website_registry import upsert_website


class CrawlData(BaseModel):
    model_config = ConfigDict(frozen=True)

    website_url: str
    site_type: str | None = None
    website_topics: list[str] = Field(default_factory=list)
    language: str | None = None
    home_page_url: str | None = None
    home_content: str | None = None
    about_page_url: str | None
    about_content: str | None
    blog_page_url: str | None = None
    blog_content: str | None = None
    service_page_url: str | None = None
    service_content: str | None = None
    contact_page_url: str | None
    contact_content: str | None
    goals_page_url: str | None
    goals_content: str | None
    emails: list[str] = Field(default_factory=list)
    social_links: list[str] = Field(default_factory=list)
    phones: list[str] = Field(default_factory=list)
    company_links: list[tuple[str, str]] = Field(default_factory=list)
    pages: list[CrawlPageClassification] = Field(default_factory=list)


async def crawl_website(
    website_url: str,
    max_pages: int | None = None,
    max_depth: int | None = None,
) -> CrawlData:
    service_url = f"{settings.crawler_service_url.rstrip('/')}/crawl"
    payload: dict[str, object] = {"website_url": website_url}
    if max_pages is not None:
        payload["max_pages"] = max_pages
    if max_depth is not None:
        payload["max_depth"] = max_depth
    try:
        async with httpx.AsyncClient(timeout=90.0) as client:
            response = await client.post(
                service_url,
                json=payload,
            )
    except httpx.TimeoutException as exc:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="The website crawler service timed out.",
        ) from exc
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The website crawler service is unavailable.",
        ) from exc

    if response.is_error:
        try:
            error_data = response.json()
        except ValueError:
            error_data = None
        detail = (
            error_data.get("detail")
            if isinstance(error_data, dict)
            else None
        )
        raise HTTPException(
            status_code=response.status_code,
            detail=detail or "The website crawler service failed.",
        )

    try:
        response_data = response.json()
        return CrawlData.model_validate(response_data)
    except (ValidationError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="The website crawler service returned an invalid response.",
        ) from exc


def _persist_crawl(
    data: CrawlData,
    db: Session,
    *,
    discovery_source: str = "manual",
    discovery_provider: str | None = None,
    registry_website_url: str | None = None,
) -> WebsiteCrawl:
    for attempt in range(2):
        try:
            crawl = db.scalar(
                select(WebsiteCrawl)
                .where(WebsiteCrawl.website_url == data.website_url)
                .limit(1)
            )
            if crawl is None:
                crawl = WebsiteCrawl(website_url=data.website_url)
                db.add(crawl)

            if data.site_type is not None:
                crawl.site_type = data.site_type
            crawl.home_page_url = data.home_page_url
            crawl.home_content = data.home_content
            crawl.about_page_url = data.about_page_url
            crawl.about_content = data.about_content
            crawl.blog_page_url = data.blog_page_url
            crawl.blog_content = data.blog_content
            crawl.service_page_url = data.service_page_url
            crawl.service_content = data.service_content
            crawl.contact_page_url = data.contact_page_url
            crawl.contact_content = data.contact_content
            crawl.goals_page_url = data.goals_page_url
            crawl.goals_content = data.goals_content
            page_signals = [
                page.model_dump(mode="json")
                for page in data.pages
                if page.classification_signals is not None
            ]
            if page_signals:
                crawl.classification_signals = page_signals
            crawl.emails = data.emails
            crawl.social_links = data.social_links
            crawl.crawled_at = datetime.now(timezone.utc)
            db.flush()
            website = upsert_website(
                db,
                registry_website_url or crawl.website_url,
                discovery_source=discovery_source,
                discovery_provider=discovery_provider,
                category=crawl.site_type,
                crawl_id=crawl.id,
                language=data.language,
            )
            if data.website_topics and not website.site_topic:
                website.site_topic = data.website_topics
            db.commit()
            db.refresh(crawl)
            return crawl
        except IntegrityError:
            db.rollback()
            if attempt == 1:
                raise
        except Exception:
            db.rollback()
            raise

    raise RuntimeError("Could not persist website crawl after retry.")


def get_website_crawl(website_url: str, db: Session) -> WebsiteCrawl | None:
    canonical_url = _normalize_website_url(website_url)
    return db.scalar(
        select(WebsiteCrawl).where(WebsiteCrawl.website_url == canonical_url)
    )


async def crawl_and_persist(
    website_url: str,
    db: Session,
) -> WebsiteCrawl:
    crawl_settings = get_crawl_settings(db)
    crawl_data = await crawl_website(
        website_url,
        max_pages=crawl_settings.max_pages,
        max_depth=crawl_settings.max_depth,
    )
    return await run_in_threadpool(_persist_crawl, crawl_data, db)
