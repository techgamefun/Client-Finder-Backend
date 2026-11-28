from datetime import datetime, timezone
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.models import (
    PageSpeedScan,
    PageSpeedScanJob,
    Website,
    WebsiteAhrefsDomainRatingJob,
    WebsiteCrawl,
    WebsiteCrawlJob,
    WebsiteHypeStatFetchJob,
    WebsiteHypeStatProfile,
)
from app.schemas.website_crawl import (
    ManualEnrichmentStatus,
    ManualWebsiteCrawlCompletionRequest,
    ManualWebsiteCrawlCompletionResponse,
)
from app.services.contact_import import add_crawl_contacts
from app.services.application_settings import (
    get_hypestat_settings,
    get_user_provider_api_key,
)
from app.services.pagespeed import PAGESPEED_STRATEGIES
from app.services.pagespeed_jobs import enqueue_pagespeed_jobs
from app.services.website_ahrefs_jobs import (
    enqueue_website_ahrefs_domain_rating_jobs,
)
from app.services.website_hypestat_jobs import enqueue_hypestat_fetch_jobs
from app.services.website_registry import upsert_website


async def complete_failed_website_crawl_manually(
    job_id: UUID,
    payload: ManualWebsiteCrawlCompletionRequest,
    db: Session,
) -> ManualWebsiteCrawlCompletionResponse:
    response, ahrefs_job_ids, hypestat_job_ids, pagespeed_job_ids = (
        await run_in_threadpool(
            _complete_failed_website_crawl_manually,
            job_id,
            payload,
            db,
        )
    )
    await enqueue_website_ahrefs_domain_rating_jobs(ahrefs_job_ids)
    await enqueue_hypestat_fetch_jobs(hypestat_job_ids)
    await enqueue_pagespeed_jobs(pagespeed_job_ids)
    return response


def _complete_failed_website_crawl_manually(
    job_id: UUID,
    payload: ManualWebsiteCrawlCompletionRequest,
    db: Session,
) -> tuple[
    ManualWebsiteCrawlCompletionResponse,
    list[UUID],
    list[UUID],
    list[UUID],
]:
    job = db.scalar(
        select(WebsiteCrawlJob)
        .where(WebsiteCrawlJob.id == job_id)
        .with_for_update()
    )
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Website indexing job not found.",
        )
    if job.status != "failed":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only failed website indexing jobs can be completed manually.",
        )

    try:
        now = datetime.now(timezone.utc)
        website = upsert_website(
            db,
            job.website_url,
            category=payload.site_type,
            language=payload.language,
        )
        website.classification_manual = True
        if payload.website_topics:
            website.site_topic = payload.website_topics

        crawl = (
            db.scalar(
                select(WebsiteCrawl).where(
                    WebsiteCrawl.id == website.crawl_id,
                )
            )
            if website.crawl_id is not None
            else None
        )
        if crawl is None:
            crawl = db.scalar(
                select(WebsiteCrawl).where(
                    WebsiteCrawl.website_url == website.website_url,
                )
            )
        if crawl is None:
            crawl = WebsiteCrawl(website_url=website.website_url)
            db.add(crawl)

        crawl.source = "manual"
        crawl.site_type = payload.site_type
        for field in (
            "home_page_url",
            "home_content",
            "about_page_url",
            "about_content",
            "blog_page_url",
            "blog_content",
            "service_page_url",
            "service_content",
            "contact_page_url",
            "contact_content",
            "goals_page_url",
            "goals_content",
        ):
            value = getattr(payload, field)
            if value is not None:
                setattr(crawl, field, str(value) if field.endswith("_url") else value)
        if payload.social_links:
            crawl.social_links = [str(link) for link in payload.social_links]
        crawl.crawled_at = now
        db.flush()

        website.crawl_id = crawl.id
        add_crawl_contacts(website, payload.emails, payload.phones)
        job.crawl_id = crawl.id
        job.status = "manual"
        job.completed_at = now
        job.updated_at = now

        ahrefs_job_ids, ahrefs_status = _queue_ahrefs_if_needed(
            website,
            job.created_by_user_id,
            now,
            db,
        )
        hypestat_job_ids, hypestat_status = _queue_hypestat_if_needed(
            website,
            job.created_by_user_id,
            db,
        )
        pagespeed_job_ids, pagespeed_status = _queue_pagespeed_if_needed(
            website,
            job.created_by_user_id,
            db,
        )
        db.commit()
    except Exception:
        db.rollback()
        raise

    return (
        ManualWebsiteCrawlCompletionResponse(
            job_id=job.id,
            website_id=website.id,
            crawl_id=crawl.id,
            status="manual",
            ahrefs_domain_rating=ahrefs_status,
            hypestat=hypestat_status,
            pagespeed=pagespeed_status,
        ),
        ahrefs_job_ids,
        hypestat_job_ids,
        pagespeed_job_ids,
    )


def _queue_ahrefs_if_needed(
    website: Website,
    user_id: UUID | None,
    now: datetime,
    db: Session,
) -> tuple[list[UUID], ManualEnrichmentStatus]:
    if website.ahrefs_domain_rating is not None:
        return [], "already_available"
    active_job = db.scalar(
        select(WebsiteAhrefsDomainRatingJob.id).where(
            WebsiteAhrefsDomainRatingJob.website_id == website.id,
            WebsiteAhrefsDomainRatingJob.status.in_(("queued", "running")),
        )
    )
    if active_job is not None:
        return [], "already_queued"
    if user_id is None or not get_user_provider_api_key(
        db,
        "ahrefs",
        user_id=user_id,
    ):
        return [], "not_configured"
    job = WebsiteAhrefsDomainRatingJob(
        website_id=website.id,
        source_crawled_at=now,
        created_by_user_id=user_id,
    )
    db.add(job)
    db.flush()
    return [job.id], "queued"


def _queue_hypestat_if_needed(
    website: Website,
    user_id: UUID | None,
    db: Session,
) -> tuple[list[UUID], ManualEnrichmentStatus]:
    profile = db.get(WebsiteHypeStatProfile, website.id)
    if profile is not None and profile.status == "succeeded":
        return [], "already_available"
    active_job = db.scalar(
        select(WebsiteHypeStatFetchJob.id).where(
            WebsiteHypeStatFetchJob.website_id == website.id,
            WebsiteHypeStatFetchJob.status.in_(("queued", "running")),
        )
    )
    if active_job is not None:
        return [], "already_queued"
    if not get_hypestat_settings(db).auto_fetch_enabled:
        return [], "disabled"
    if user_id is None or not get_user_provider_api_key(
        db,
        "hypestat",
        user_id=user_id,
    ):
        return [], "not_configured"

    if profile is None:
        profile = WebsiteHypeStatProfile(
            website_id=website.id,
            status="pending",
        )
        db.add(profile)
    else:
        profile.status = "pending"
        profile.error = None
    job = WebsiteHypeStatFetchJob(
        website_id=website.id,
        created_by_user_id=user_id,
    )
    db.add(job)
    db.flush()
    return [job.id], "queued"


def _queue_pagespeed_if_needed(
    website: Website,
    user_id: UUID | None,
    db: Session,
) -> tuple[list[UUID], ManualEnrichmentStatus]:
    completed = set(
        db.scalars(
            select(PageSpeedScan.strategy).where(
                PageSpeedScan.website_id == website.id,
            )
        ).all()
    )
    active = set(
        db.scalars(
            select(PageSpeedScanJob.strategy).where(
                PageSpeedScanJob.website_id == website.id,
                PageSpeedScanJob.status.in_(("queued", "running")),
            )
        ).all()
    )
    missing = [
        strategy
        for strategy in PAGESPEED_STRATEGIES
        if strategy not in completed and strategy not in active
    ]
    if not missing:
        return [], "already_queued" if active else "already_available"
    if user_id is None or not get_user_provider_api_key(
        db,
        "pagespeed",
        user_id=user_id,
    ):
        return [], "not_configured"

    jobs = [
        PageSpeedScanJob(
            website_id=website.id,
            website_url=website.website_url,
            strategy=strategy,
            created_by_user_id=user_id,
        )
        for strategy in missing
    ]
    db.add_all(jobs)
    db.flush()
    return [job.id for job in jobs], "queued"
