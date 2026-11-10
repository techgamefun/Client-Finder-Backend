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
from app.services.public_url import _normalize_website_url


class CrawlData(BaseModel):
    model_config = ConfigDict(frozen=True)

    website_url: str
    about_page_url: str | None
    about_content: str | None
    contact_page_url: str | None
    contact_content: str | None
    goals_page_url: str | None
    goals_content: str | None
    emails: list[str] = Field(default_factory=list)
    social_links: list[str] = Field(default_factory=list)
    phones: list[str] = Field(default_factory=list)
    company_links: list[tuple[str, str]] = Field(default_factory=list)


async def crawl_website(website_url: str) -> CrawlData:
    service_url = f"{settings.crawler_service_url.rstrip('/')}/crawl"
    try:
        async with httpx.AsyncClient(timeout=90.0) as client:
            response = await client.post(
                service_url,
                json={"website_url": website_url},
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


def _persist_crawl(data: CrawlData, db: Session) -> WebsiteCrawl:
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

            crawl.about_page_url = data.about_page_url
            crawl.about_content = data.about_content
            crawl.contact_page_url = data.contact_page_url
            crawl.contact_content = data.contact_content
            crawl.goals_page_url = data.goals_page_url
            crawl.goals_content = data.goals_content
            crawl.emails = data.emails
            crawl.social_links = data.social_links
            crawl.crawled_at = datetime.now(timezone.utc)
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
    crawl_data = await crawl_website(website_url)
    return await run_in_threadpool(_persist_crawl, crawl_data, db)
