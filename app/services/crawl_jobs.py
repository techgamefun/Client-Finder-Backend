import logging
from datetime import datetime, timezone
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import WebsiteCrawlJob
from app.schemas.website_crawl import WebsiteCrawlJobResponse
from app.services.redis_queue import RedisQueueError, enqueue_job


logger = logging.getLogger(__name__)


async def create_website_crawl_job(
    website_url: str,
    db: Session,
) -> WebsiteCrawlJob:
    job = WebsiteCrawlJob(website_url=website_url, status="queued")
    db.add(job)
    db.commit()
    db.refresh(job)

    try:
        await enqueue_job(str(job.id))
    except RedisQueueError as exc:
        logger.error("Could not dispatch website crawl job %s.", job.id)
        job.status = "failed"
        job.error = "The crawl queue is unavailable. Please try again."
        job.completed_at = datetime.now(timezone.utc)
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=job.error,
        ) from exc
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
        completed_at=job.completed_at,
        result=job.crawl,
    )
