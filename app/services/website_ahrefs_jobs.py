import logging
from datetime import datetime, timezone
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.models import Website, WebsiteAhrefsDomainRatingJob
from app.schemas.website import WebsiteBulkEnrichmentResponse
from app.services.application_settings import get_user_provider_api_key
from app.services.redis_queue import (
    RedisQueueError,
    enqueue_ahrefs_domain_rating_jobs as push_ahrefs_domain_rating_jobs,
)


logger = logging.getLogger(__name__)


def create_website_ahrefs_domain_rating_job(
    website_id: UUID,
    source_crawled_at: datetime,
    db: Session,
) -> WebsiteAhrefsDomainRatingJob | None:
    existing = db.scalar(
        select(WebsiteAhrefsDomainRatingJob).where(
            WebsiteAhrefsDomainRatingJob.website_id == website_id,
            WebsiteAhrefsDomainRatingJob.source_crawled_at == source_crawled_at,
        )
    )
    if existing is not None:
        return None

    job = WebsiteAhrefsDomainRatingJob(
        website_id=website_id,
        source_crawled_at=source_crawled_at,
    )
    try:
        with db.begin_nested():
            db.add(job)
            db.flush()
    except IntegrityError:
        return None
    return job


async def enqueue_website_ahrefs_domain_rating_jobs(
    job_ids: list[UUID],
) -> None:
    if not job_ids:
        return
    try:
        await push_ahrefs_domain_rating_jobs(
            [str(job_id) for job_id in job_ids]
        )
    except RedisQueueError:
        logger.exception(
            "Could not dispatch Ahrefs Domain Rating jobs; queued jobs "
            "remain available for recovery."
        )


async def queue_missing_website_ahrefs_domain_ratings(
    db: Session,
) -> WebsiteBulkEnrichmentResponse:
    response, job_ids = await run_in_threadpool(
        _queue_missing_website_ahrefs_domain_ratings,
        db,
    )
    await enqueue_website_ahrefs_domain_rating_jobs(job_ids)
    return response


def _queue_missing_website_ahrefs_domain_ratings(
    db: Session,
) -> tuple[WebsiteBulkEnrichmentResponse, list[UUID]]:
    websites = db.scalars(select(Website).order_by(Website.id)).all()
    if not websites:
        return (
            WebsiteBulkEnrichmentResponse(
                website_count=0,
                queued_website_count=0,
                queued_job_count=0,
                missing_website_count=0,
                in_progress_job_count=0,
                skipped_website_count=0,
            ),
            [],
        )

    missing_websites = [
        website for website in websites
        if website.ahrefs_domain_rating is None
    ]
    if not missing_websites:
        return (
            WebsiteBulkEnrichmentResponse(
                website_count=len(websites),
                queued_website_count=0,
                queued_job_count=0,
                missing_website_count=0,
                in_progress_job_count=0,
                skipped_website_count=len(websites),
            ),
            [],
        )
    if not get_user_provider_api_key(db, "ahrefs"):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Ahrefs is not configured. Add your API key in Settings.",
        )

    website_ids = [website.id for website in missing_websites]
    active_website_ids = set(
        db.scalars(
            select(WebsiteAhrefsDomainRatingJob.website_id).where(
                WebsiteAhrefsDomainRatingJob.website_id.in_(website_ids),
                WebsiteAhrefsDomainRatingJob.status.in_(("queued", "running")),
            )
        ).all()
    )
    now = datetime.now(timezone.utc)
    jobs = [
        WebsiteAhrefsDomainRatingJob(
            website_id=website.id,
            source_crawled_at=now,
        )
        for website in missing_websites
        if website.id not in active_website_ids
    ]
    db.add_all(jobs)
    db.flush()
    job_ids = [job.id for job in jobs]
    queued_website_count = len(jobs)
    db.commit()
    return (
        WebsiteBulkEnrichmentResponse(
            website_count=len(websites),
            queued_website_count=queued_website_count,
            queued_job_count=len(job_ids),
            missing_website_count=len(missing_websites),
            in_progress_job_count=len(active_website_ids),
            skipped_website_count=len(websites) - queued_website_count,
        ),
        job_ids,
    )
