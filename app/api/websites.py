from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.api.deps import get_db
from app.models import Website, WebsiteCrawlJob
from app.schemas.website import (
    WebsiteCrawlJobHistoryResponse,
    WebsiteDetailResponse,
    WebsiteResponse,
    WebsiteUpdate,
)
from app.schemas.website_crawl import (
    CrawlPageClassification,
    WebsiteCrawlResponse,
)


router = APIRouter(prefix="/websites", tags=["Websites"])

WebsiteSession = Annotated[Session, Depends(get_db)]


@router.get("", response_model=list[WebsiteResponse])
def list_websites(
    db: WebsiteSession,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    site_type: Literal["normal", "listing"] | None = None,
    page_type: str | None = Query(default=None, max_length=64),
    search: Annotated[str, Query(max_length=300)] = "",
) -> list[WebsiteResponse]:
    statement = (
        select(Website)
        .options(selectinload(Website.crawl))
        .order_by(Website.created_at.desc(), Website.id.desc())
        .limit(limit)
        .offset(offset)
    )
    if site_type is not None:
        statement = statement.where(Website.site_type == site_type)
    if page_type is not None:
        statement = statement.where(Website.page_type == page_type)
    if search:
        statement = statement.where(
            Website.website_url.contains(search, autoescape=True)
            | Website.domain.contains(search, autoescape=True)
        )

    websites = list(db.scalars(statement).all())
    crawl_job_statuses = _crawl_job_statuses(db, websites)
    return [
        _website_response(
            website,
            crawl_job_statuses.get(website.website_url),
        )
        for website in websites
    ]


@router.get("/{website_id}", response_model=WebsiteDetailResponse)
def get_website_detail(
    website_id: UUID,
    db: WebsiteSession,
) -> WebsiteDetailResponse:
    website = db.scalar(
        select(Website)
        .options(selectinload(Website.crawl))
        .where(Website.id == website_id)
    )
    if website is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Website not found.",
        )

    crawl_jobs = db.scalars(
        select(WebsiteCrawlJob)
        .where(WebsiteCrawlJob.website_url == website.website_url)
        .order_by(WebsiteCrawlJob.created_at.desc(), WebsiteCrawlJob.id.desc())
    ).all()
    crawl_job_status = crawl_jobs[0].status if crawl_jobs else None
    return WebsiteDetailResponse(
        website=_website_response(website, crawl_job_status),
        crawl=(
            WebsiteCrawlResponse.model_validate(website.crawl)
            if website.crawl is not None
            else None
        ),
        crawl_jobs=[
            WebsiteCrawlJobHistoryResponse.model_validate(job)
            for job in crawl_jobs
        ],
    )


@router.patch("/{website_id}", response_model=WebsiteResponse)
def update_website(
    website_id: UUID,
    payload: WebsiteUpdate,
    db: WebsiteSession,
) -> WebsiteResponse:
    website = db.scalar(
        select(Website)
        .options(selectinload(Website.crawl))
        .where(Website.id == website_id)
    )
    if website is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Website not found.",
        )

    changes = payload.model_dump(exclude_unset=True)
    if "page_type" in changes:
        website.classification_manual = True
        if changes["page_type"] == "business_listing":
            website.site_type = "listing"
            website.discovery_source = "listing_website"
        else:
            website.site_type = "normal"
            if website.discovery_source == "listing_website":
                website.discovery_source = website.discovery_provider or "manual"
    for field, value in changes.items():
        setattr(website, field, value)
    db.commit()
    db.refresh(website)
    crawl_job_status = _crawl_job_statuses(db, [website]).get(website.website_url)
    return _website_response(website, crawl_job_status)


def _crawl_job_statuses(
    db: Session,
    websites: list[Website],
) -> dict[str, str]:
    website_urls = {website.website_url for website in websites}
    if not website_urls:
        return {}

    jobs = db.scalars(
        select(WebsiteCrawlJob)
        .where(WebsiteCrawlJob.website_url.in_(website_urls))
        .order_by(WebsiteCrawlJob.created_at.desc(), WebsiteCrawlJob.id.desc())
    ).all()
    statuses: dict[str, str] = {}
    for job in jobs:
        statuses.setdefault(job.website_url, job.status)
    return statuses


def _website_response(
    website: Website,
    crawl_job_status: str | None,
) -> WebsiteResponse:
    crawl_signals = [
        CrawlPageClassification.model_validate(page)
        for page in (
            website.crawl.classification_signals
            if website.crawl is not None
            else []
        )
    ]
    return WebsiteResponse(
        id=website.id,
        website_url=website.website_url,
        domain=website.domain,
        crawl_id=website.crawl_id,
        is_crawled=website.crawl_id is not None,
        crawl_job_status=(
            crawl_job_status
            or ("succeeded" if website.crawl_id is not None else None)
        ),
        category=website.category,
        page_type=website.page_type,
        site_topic=website.site_topic,
        language=website.language,
        status=website.status,
        site_type=website.site_type,
        discovery_source=website.discovery_source,
        discovery_provider=website.discovery_provider,
        created_at=website.created_at,
        crawled_at=website.crawl.crawled_at if website.crawl else None,
        crawl_signals=crawl_signals,
    )
