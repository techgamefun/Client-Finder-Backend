import logging
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.config import settings
from app.models import (
    Website,
    WebsiteHypeStatFetchJob,
    WebsiteHypeStatProfile,
)
from app.schemas.website import WebsiteBulkEnrichmentResponse
from app.services.application_settings import get_hypestat_settings
from app.services.redis_queue import (
    RedisQueueError,
    enqueue_hypestat_fetch_jobs as push_hypestat_fetch_jobs,
)


logger = logging.getLogger(__name__)


async def queue_missing_website_hypestat_profiles(
    db: Session,
) -> WebsiteBulkEnrichmentResponse:
    response, job_ids = await run_in_threadpool(
        _queue_missing_website_hypestat_profiles,
        db,
    )
    if job_ids:
        try:
            await push_hypestat_fetch_jobs([str(job_id) for job_id in job_ids])
        except RedisQueueError:
            logger.exception(
                "Could not dispatch HypeStat fetch jobs; queued jobs remain "
                "available for recovery."
            )
    return response


def _queue_missing_website_hypestat_profiles(
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

    hypestat_settings = get_hypestat_settings(db)
    if not hypestat_settings.api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="HypeStat is not configured. Add your API key in Settings.",
        )
    if not settings.crawler_service_shared_token:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The crawler service is not configured for HypeStat.",
        )

    website_ids = [website.id for website in websites]
    profiles = {
        profile.website_id: profile
        for profile in db.scalars(
            select(WebsiteHypeStatProfile).where(
                WebsiteHypeStatProfile.website_id.in_(website_ids)
            )
        ).all()
    }
    active_website_ids = set(
        db.scalars(
            select(WebsiteHypeStatFetchJob.website_id).where(
                WebsiteHypeStatFetchJob.website_id.in_(website_ids),
                WebsiteHypeStatFetchJob.status.in_(("queued", "running")),
            )
        ).all()
    )

    jobs: list[WebsiteHypeStatFetchJob] = []
    queued_website_ids: set[UUID] = set()
    missing_website_ids: set[UUID] = set()
    for website in websites:
        profile = profiles.get(website.id)
        if (
            profile is not None
            and profile.status == "succeeded"
            and profile.payload is not None
        ):
            continue
        missing_website_ids.add(website.id)
        if website.id in active_website_ids:
            continue

        if profile is None:
            profile = WebsiteHypeStatProfile(
                website_id=website.id,
                status="pending",
            )
            db.add(profile)
        else:
            profile.status = "pending"
            profile.error = None

        jobs.append(WebsiteHypeStatFetchJob(website_id=website.id))
        queued_website_ids.add(website.id)

    db.add_all(jobs)
    db.flush()
    job_ids = [job.id for job in jobs]
    db.commit()
    return (
        WebsiteBulkEnrichmentResponse(
            website_count=len(websites),
            queued_website_count=len(queued_website_ids),
            queued_job_count=len(job_ids),
            missing_website_count=len(missing_website_ids),
            in_progress_job_count=len(
                missing_website_ids & active_website_ids
            ),
            skipped_website_count=len(websites) - len(queued_website_ids),
        ),
        job_ids,
    )
