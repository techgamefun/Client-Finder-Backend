import logging
from datetime import datetime, timezone
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import WebsiteCrawlJob
from app.schemas.website_crawl import WebsiteCrawlJobResponse
from app.services.redis_queue import RedisQueueError, enqueue_job
from app.services.website_registry import upsert_website


logger = logging.getLogger(__name__)


async def create_website_crawl_job(
    website_url: str,
    db: Session,
    *,
    discovery_source: str = "manual",
    discovery_provider: str | None = None,
) -> WebsiteCrawlJob:
    website = upsert_website(
        db,
        website_url,
        discovery_source=discovery_source,
        discovery_provider=discovery_provider,
    )
    job = WebsiteCrawlJob(website_url=website.website_url, status="queued")
    db.add(job)
    db.commit()
    db.refresh(job)

    try:
        await enqueue_job(str(job.id))
    except RedisQueueError:
        logger.exception(
            "Could not dispatch website crawl job %s; it remains queued for "
            "worker recovery.",
            job.id,
        )
    return job


def get_website_crawl_job(
    job_id: UUID,
    db: Session,
) -> WebsiteCrawlJobResponse:
    job = db.scalar(
        select(WebsiteCrawlJob).where(WebsiteCrawlJob.id == job_id)
    )
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Website crawl job not found.",
        )
    return WebsiteCrawlJobResponse(
        id=job.id,
        website_url=job.website_url,
        status=job.status,
        attempts=job.attempts,
        error=job.error,
        created_at=job.created_at,
        updated_at=job.updated_at,
        started_at=job.started_at,
        completed_at=job.completed_at,
        result=job.crawl,
    )


async def retry_website_crawl_job(
    job_id: UUID,
    db: Session,
) -> WebsiteCrawlJobResponse:
    job = db.scalar(
        select(WebsiteCrawlJob)
        .where(WebsiteCrawlJob.id == job_id)
        .with_for_update()
    )
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Website crawl job not found.",
        )
    if job.status != "failed":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only failed website crawl jobs can be retried.",
        )

    now = datetime.now(timezone.utc)
    job.status = "queued"
    job.attempts = 0
    job.error = None
    job.started_at = None
    job.completed_at = None
    job.updated_at = now
    db.commit()

    try:
        await enqueue_job(str(job.id))
    except RedisQueueError:
        logger.exception(
            "Could not dispatch retried website crawl job %s; it remains "
            "queued for worker recovery.",
            job.id,
        )
    return get_website_crawl_job(job.id, db)


def list_website_crawl_jobs(
    db: Session,
    limit: int = 100,
) -> list[WebsiteCrawlJobResponse]:
    jobs = db.scalars(
        select(WebsiteCrawlJob)
        .options(selectinload(WebsiteCrawlJob.crawl))
        .order_by(WebsiteCrawlJob.created_at.desc(), WebsiteCrawlJob.id.desc())
        .limit(limit)
    ).all()
    return [
        WebsiteCrawlJobResponse(
            id=job.id,
            website_url=job.website_url,
            status=job.status,
            attempts=job.attempts,
            error=job.error,
            created_at=job.created_at,
            updated_at=job.updated_at,
            started_at=job.started_at,
            completed_at=job.completed_at,
            result=job.crawl,
        )
        for job in jobs
    ]
