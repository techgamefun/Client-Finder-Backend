import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload
from starlette.concurrency import run_in_threadpool

from app.models import WebsiteCrawlJob
from app.schemas.job_queue import (
    BatchJobRetryFailure,
    BatchJobRetryResponse,
    JobQueueStatusSummary,
    QueueJobsPageResponse,
)
from app.schemas.website_crawl import (
    WebsiteCrawlJobResponse,
)
from app.services.normalization import normalize_domain
from app.services.excluded_domains import find_excluded_domain
from app.services.redis_queue import (
    RedisQueueError,
    enqueue_crawl_jobs,
    enqueue_job,
)
from app.services.website_registry import upsert_website


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExcludedCrawlTarget:
    website_url: str
    reason: str


async def create_website_crawl_jobs(
    website_urls: list[str],
    db: Session,
) -> tuple[list[WebsiteCrawlJob], list[ExcludedCrawlTarget]]:
    jobs, excluded = await run_in_threadpool(
        _create_website_crawl_jobs_sync,
        website_urls,
        db,
    )
    job_ids = [str(job.id) for job in jobs]
    if job_ids:
        try:
            await enqueue_crawl_jobs(job_ids)
        except RedisQueueError:
            logger.exception(
                "Could not dispatch %s website crawl jobs; they remain queued "
                "for worker recovery.",
                len(job_ids),
            )
    return jobs, excluded


def _create_website_crawl_jobs_sync(
    website_urls: list[str],
    db: Session,
) -> tuple[list[WebsiteCrawlJob], list[ExcludedCrawlTarget]]:
    jobs = []
    excluded = []
    seen_domains: set[str] = set()

    for website_url in website_urls:
        domain = normalize_domain(website_url)
        if domain is None:
            raise ValueError(
                f"Could not determine a website domain from {website_url!r}."
            )
        if domain in seen_domains:
            continue
        seen_domains.add(domain)

        exclusion = find_excluded_domain(db, website_url)
        if exclusion is not None:
            excluded.append(
                ExcludedCrawlTarget(
                    website_url=website_url,
                    reason=exclusion.reason,
                )
            )
            continue

        website = upsert_website(
            db,
            website_url,
            discovery_source="manual",
        )
        job = WebsiteCrawlJob(website_url=website.website_url, status="queued")
        db.add(job)
        jobs.append(job)

    db.commit()
    return jobs, excluded


async def create_website_crawl_job(
    website_url: str,
    db: Session,
    *,
    discovery_source: str = "manual",
    discovery_provider: str | None = None,
) -> WebsiteCrawlJob:
    job = await run_in_threadpool(
        _create_website_crawl_job_sync,
        website_url,
        db,
        discovery_source=discovery_source,
        discovery_provider=discovery_provider,
    )
    if job.status == "queued":
        try:
            await enqueue_job(str(job.id))
        except RedisQueueError:
            logger.exception(
                "Could not dispatch website crawl job %s; it remains queued for "
                "worker recovery.",
                job.id,
            )
    return job


def _create_website_crawl_job_sync(
    website_url: str,
    db: Session,
    *,
    discovery_source: str = "manual",
    discovery_provider: str | None = None,
) -> WebsiteCrawlJob:
    exclusion = find_excluded_domain(db, website_url)
    if exclusion is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"This domain is excluded: {exclusion.reason}.",
        )

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
    retried_job_id = await run_in_threadpool(
        _retry_website_crawl_job_sync,
        job_id,
        db,
    )
    try:
        await enqueue_job(str(retried_job_id))
    except RedisQueueError:
        logger.exception(
            "Could not dispatch retried website crawl job %s; it remains "
            "queued for worker recovery.",
            retried_job_id,
        )
    return await run_in_threadpool(get_website_crawl_job, retried_job_id, db)


def _retry_website_crawl_job_sync(
    job_id: UUID,
    db: Session,
) -> UUID:
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
    exclusion = find_excluded_domain(db, job.website_url)
    if exclusion is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"This domain is excluded: {exclusion.reason}.",
        )

    now = datetime.now(timezone.utc)
    job.status = "queued"
    job.attempts = 0
    job.error = None
    job.started_at = None
    job.completed_at = None
    job.updated_at = now
    db.commit()
    return job.id


async def retry_failed_website_crawl_jobs(
    job_ids: list[UUID],
    db: Session,
) -> BatchJobRetryResponse:
    response, retried_job_ids = await run_in_threadpool(
        _retry_failed_website_crawl_jobs_sync,
        job_ids,
        db,
    )
    if retried_job_ids:
        try:
            await enqueue_crawl_jobs(
                [str(job_id) for job_id in retried_job_ids]
            )
        except RedisQueueError:
            logger.exception(
                "Could not dispatch %s retried website crawl jobs; they "
                "remain queued for worker recovery.",
                len(retried_job_ids),
            )
    return response


def _retry_failed_website_crawl_jobs_sync(
    job_ids: list[UUID],
    db: Session,
) -> tuple[BatchJobRetryResponse, list[UUID]]:
    unique_job_ids = list(dict.fromkeys(job_ids))
    jobs = db.scalars(
        select(WebsiteCrawlJob)
        .where(WebsiteCrawlJob.id.in_(unique_job_ids))
        .with_for_update()
    ).all()
    jobs_by_id = {job.id: job for job in jobs}
    retried_job_ids: list[UUID] = []
    failures: list[BatchJobRetryFailure] = []
    now = datetime.now(timezone.utc)

    for job_id in unique_job_ids:
        job = jobs_by_id.get(job_id)
        if job is None:
            failures.append(
                BatchJobRetryFailure(
                    job_id=job_id,
                    message="Website crawl job not found.",
                )
            )
        elif job.status != "failed":
            failures.append(
                BatchJobRetryFailure(
                    job_id=job_id,
                    message="Only failed website crawl jobs can be retried.",
                )
            )
        elif (exclusion := find_excluded_domain(db, job.website_url)) is not None:
            failures.append(
                BatchJobRetryFailure(
                    job_id=job_id,
                    message=f"This domain is excluded: {exclusion.reason}.",
                )
            )
        else:
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


def list_website_crawl_jobs(
    db: Session,
    *,
    search: str = "",
    status: str | None = None,
    page: int = 1,
    page_size: int = 25,
) -> QueueJobsPageResponse[WebsiteCrawlJobResponse]:
    filters = []
    if search:
        filters.append(
            func.lower(WebsiteCrawlJob.website_url).contains(
                search.lower(),
                autoescape=True,
            )
        )
    status_counts = dict(
        db.execute(
            select(WebsiteCrawlJob.status, func.count())
            .where(*filters)
            .group_by(WebsiteCrawlJob.status)
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
        filters.append(WebsiteCrawlJob.status == status)
    jobs = db.scalars(
        select(WebsiteCrawlJob)
        .options(selectinload(WebsiteCrawlJob.crawl))
        .order_by(WebsiteCrawlJob.created_at.desc(), WebsiteCrawlJob.id.desc())
        .where(*filters)
        .limit(page_size)
        .offset((page - 1) * page_size)
    ).all()
    return QueueJobsPageResponse(
        items=[
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
        ],
        total_count=total_count,
        page=page,
        page_size=page_size,
        status_counts=status_counts,
    )


def summarize_website_crawl_jobs(db: Session) -> JobQueueStatusSummary:
    counts = dict(
        db.execute(
            select(WebsiteCrawlJob.status, func.count())
            .where(WebsiteCrawlJob.status.in_(("running", "failed")))
            .group_by(WebsiteCrawlJob.status)
        ).all()
    )
    return JobQueueStatusSummary(
        running_count=counts.get("running", 0),
        failed_count=counts.get("failed", 0),
    )
