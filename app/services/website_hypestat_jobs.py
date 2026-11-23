import logging
from datetime import datetime, timezone
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.config import settings
from app.models import (
    Website,
    WebsiteHypeStatFetchJob,
    WebsiteHypeStatProfile,
)
from app.schemas.job_queue import (
    BatchJobRetryFailure,
    BatchJobRetryResponse,
    QueueJobsPageResponse,
)
from app.schemas.website import (
    WebsiteBulkEnrichmentResponse,
    WebsiteHypeStatFetchJobResponse,
)
from app.services.application_settings import (
    get_hypestat_settings,
    get_user_provider_api_key,
)
from app.services.redis_queue import (
    RedisQueueError,
    enqueue_hypestat_fetch_jobs as push_hypestat_fetch_jobs,
)


logger = logging.getLogger(__name__)
MISSING_HYPESTAT_KEY = (
    "The user who created this HypeStat job must configure a HypeStat API key "
    "before it can be retried."
)


async def queue_missing_website_hypestat_profiles(
    db: Session,
) -> WebsiteBulkEnrichmentResponse:
    response, job_ids = await run_in_threadpool(
        _queue_missing_website_hypestat_profiles,
        db,
    )
    await enqueue_hypestat_fetch_jobs(job_ids)
    return response


async def enqueue_hypestat_fetch_jobs(job_ids: list[UUID]) -> None:
    if not job_ids:
        return
    try:
        await push_hypestat_fetch_jobs([str(job_id) for job_id in job_ids])
    except RedisQueueError:
        logger.exception(
            "Could not dispatch HypeStat fetch jobs; queued jobs will be "
            "picked up by worker recovery."
        )


def serialize_hypestat_fetch_job(
    job: WebsiteHypeStatFetchJob,
    website: Website,
) -> WebsiteHypeStatFetchJobResponse:
    return WebsiteHypeStatFetchJobResponse(
        id=job.id,
        website_id=website.id,
        website_url=website.website_url,
        domain=website.domain,
        status=job.status,
        attempts=job.attempts,
        error=job.error,
        created_at=job.created_at,
        updated_at=job.updated_at,
        started_at=job.started_at,
        completed_at=job.completed_at,
    )


def list_hypestat_fetch_jobs(
    db: Session,
    *,
    search: str = "",
    status: str | None = None,
    page: int = 1,
    page_size: int = 25,
) -> QueueJobsPageResponse[WebsiteHypeStatFetchJobResponse]:
    filters = []
    if search:
        filters.append(
            func.lower(Website.website_url).contains(
                search.lower(),
                autoescape=True,
            )
        )
    status_counts = dict(
        db.execute(
            select(WebsiteHypeStatFetchJob.status, func.count())
            .join(
                Website,
                Website.id == WebsiteHypeStatFetchJob.website_id,
            )
            .where(*filters)
            .group_by(WebsiteHypeStatFetchJob.status)
        ).all()
    )
    total_count = (
        status_counts.get(status, 0)
        if status is not None
        else sum(status_counts.values())
    )
    total_pages = max(1, (total_count + page_size - 1) // page_size)
    page = min(page, total_pages)
    if status is not None:
        filters.append(WebsiteHypeStatFetchJob.status == status)
    rows = db.execute(
        select(WebsiteHypeStatFetchJob, Website)
        .join(Website, Website.id == WebsiteHypeStatFetchJob.website_id)
        .where(*filters)
        .order_by(
            WebsiteHypeStatFetchJob.created_at.desc(),
            WebsiteHypeStatFetchJob.id.desc(),
        )
        .limit(page_size)
        .offset((page - 1) * page_size)
    ).all()
    return QueueJobsPageResponse(
        items=[
            serialize_hypestat_fetch_job(job, website)
            for job, website in rows
        ],
        total_count=total_count,
        page=page,
        page_size=page_size,
        status_counts=status_counts,
    )


async def retry_hypestat_fetch_job(
    job_id: UUID,
    db: Session,
) -> WebsiteHypeStatFetchJobResponse:
    job_id, response = await run_in_threadpool(
        _retry_hypestat_fetch_job_sync,
        job_id,
        db,
    )
    await enqueue_hypestat_fetch_jobs([job_id])
    return response


def _retry_hypestat_fetch_job_sync(
    job_id: UUID,
    db: Session,
) -> tuple[UUID, WebsiteHypeStatFetchJobResponse]:
    row = db.execute(
        select(WebsiteHypeStatFetchJob, Website)
        .join(Website, Website.id == WebsiteHypeStatFetchJob.website_id)
        .where(WebsiteHypeStatFetchJob.id == job_id)
        .with_for_update()
    ).first()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="HypeStat profile job not found.",
        )
    job, website = row
    if job.status != "failed":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only failed HypeStat profile jobs can be retried.",
        )
    if job.created_by_user_id is None or not get_user_provider_api_key(
        db,
        "hypestat",
        user_id=job.created_by_user_id,
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MISSING_HYPESTAT_KEY,
        )

    profile = db.get(WebsiteHypeStatProfile, website.id)
    if profile is None:
        profile = WebsiteHypeStatProfile(
            website_id=website.id,
            status="pending",
        )
        db.add(profile)
    else:
        profile.status = "pending"
        profile.error = None

    job.status = "queued"
    job.attempts = 0
    job.error = None
    job.started_at = None
    job.completed_at = None
    job.updated_at = datetime.now(timezone.utc)
    db.commit()
    return job.id, serialize_hypestat_fetch_job(job, website)


async def retry_failed_hypestat_fetch_jobs(
    job_ids: list[UUID],
    db: Session,
) -> BatchJobRetryResponse:
    response, retried_job_ids = await run_in_threadpool(
        _retry_failed_hypestat_fetch_jobs_sync,
        job_ids,
        db,
    )
    if retried_job_ids:
        await enqueue_hypestat_fetch_jobs(retried_job_ids)
    return response


def _retry_failed_hypestat_fetch_jobs_sync(
    job_ids: list[UUID],
    db: Session,
) -> tuple[BatchJobRetryResponse, list[UUID]]:
    unique_job_ids = list(dict.fromkeys(job_ids))
    rows = db.execute(
        select(WebsiteHypeStatFetchJob, Website)
        .join(Website, Website.id == WebsiteHypeStatFetchJob.website_id)
        .where(WebsiteHypeStatFetchJob.id.in_(unique_job_ids))
        .with_for_update()
    ).all()
    jobs_by_id = {job.id: (job, website) for job, website in rows}
    retried_job_ids: list[UUID] = []
    failures: list[BatchJobRetryFailure] = []
    now = datetime.now(timezone.utc)

    for job_id in unique_job_ids:
        row = jobs_by_id.get(job_id)
        if row is None:
            failures.append(
                BatchJobRetryFailure(
                    job_id=job_id,
                    message="HypeStat profile job not found.",
                )
            )
            continue
        job, website = row
        if job.status != "failed":
            failures.append(
                BatchJobRetryFailure(
                    job_id=job_id,
                    message="Only failed HypeStat profile jobs can be retried.",
                )
            )
            continue
        if job.created_by_user_id is None or not get_user_provider_api_key(
            db,
            "hypestat",
            user_id=job.created_by_user_id,
        ):
            failures.append(
                BatchJobRetryFailure(
                    job_id=job_id,
                    message=MISSING_HYPESTAT_KEY,
                )
            )
            continue

        profile = db.get(WebsiteHypeStatProfile, website.id)
        if profile is None:
            db.add(
                WebsiteHypeStatProfile(
                    website_id=website.id,
                    status="pending",
                )
            )
        else:
            profile.status = "pending"
            profile.error = None
        job.status = "queued"
        job.attempts = 0
        job.error = None
        job.started_at = None
        job.completed_at = None
        job.updated_at = now
        retried_job_ids.append(job_id)

    db.commit()
    return (
        BatchJobRetryResponse(
            retried_job_ids=retried_job_ids,
            failures=failures,
        ),
        retried_job_ids,
    )


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
