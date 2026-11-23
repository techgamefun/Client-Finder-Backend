import logging
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import WebsiteAhrefsDomainRatingJob
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
