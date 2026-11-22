import logging
from datetime import datetime, timezone
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.models import (
    Company,
    PageSpeedScanJob,
    UserProviderCredential,
    Website,
)
from app.schemas.job_queue import (
    BatchJobRetryFailure,
    BatchJobRetryResponse,
    JobQueueStatusSummary,
)
from app.schemas.pagespeed import PageSpeedScanJobResponse
from app.services.normalization import normalize_domain
from app.services.application_settings import get_user_provider_api_key
from app.services.pagespeed import PAGESPEED_STRATEGIES
from app.services.redis_queue import (
    RedisQueueError,
    enqueue_pagespeed_jobs as push_pagespeed_jobs,
)


logger = logging.getLogger(__name__)
MISSING_PAGESPEED_KEY = (
    "The user who created this PageSpeed job must configure a Google "
    "PageSpeed API key before it can be queued."
)


def create_website_pagespeed_jobs(
    website_url: str,
    db: Session,
) -> list[UUID]:
    domain = normalize_domain(website_url)
    if domain is None:
        raise ValueError("A valid website domain is required for PageSpeed scans.")

    website = db.scalar(
        select(Website).where(Website.domain == domain).limit(1)
    )
    if website is None:
        raise RuntimeError(
            f"Website registry record for {domain} was not created by the crawl."
        )
    company = db.scalar(
        select(Company).where(Company.domain == domain).limit(1)
    )

    jobs = [
        PageSpeedScanJob(
            company_id=company.id if company is not None else None,
            website_id=website.id,
            website_url=website.website_url,
            strategy=strategy,
            companies=[company] if company is not None else [],
        )
        for strategy in PAGESPEED_STRATEGIES
    ]
    db.add_all(jobs)
    db.flush()
    return [job.id for job in jobs]


async def enqueue_pagespeed_jobs(job_ids: list[UUID]) -> None:
    if not job_ids:
        return

    try:
        await push_pagespeed_jobs([str(job_id) for job_id in job_ids])
    except RedisQueueError:
        logger.exception(
            "Could not dispatch PageSpeed scan jobs; queued jobs will be "
            "picked up by worker recovery."
        )


def serialize_pagespeed_scan_job(
    job: PageSpeedScanJob,
) -> PageSpeedScanJobResponse:
    return PageSpeedScanJobResponse(
        id=job.id,
        company_ids=[company.id for company in job.companies],
        website_id=job.website_id,
        website_url=job.website_url,
        strategy=job.strategy,
        status=job.status,
        attempts=job.attempts,
        error=job.error,
        created_at=job.created_at,
        updated_at=job.updated_at,
        started_at=job.started_at,
        completed_at=job.completed_at,
    )


def _job_has_pagespeed_key(job: PageSpeedScanJob, db: Session) -> bool:
    if job.created_by_user_id is None:
        return False
    return bool(
        get_user_provider_api_key(
            db,
            "pagespeed",
            user_id=job.created_by_user_id,
        )
    )


def get_pagespeed_scan_job(
    company_id: UUID,
    job_id: UUID,
    db: Session,
) -> PageSpeedScanJobResponse:
    job = db.scalar(
        select(PageSpeedScanJob).where(
            PageSpeedScanJob.id == job_id,
            PageSpeedScanJob.companies.any(id=company_id),
        )
    )
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="PageSpeed scan job not found.",
        )

    return serialize_pagespeed_scan_job(job)


async def retry_pagespeed_scan_job(
    job_id: UUID,
    db: Session,
) -> PageSpeedScanJobResponse:
    job = db.scalar(
        select(PageSpeedScanJob)
        .options(selectinload(PageSpeedScanJob.companies))
        .where(PageSpeedScanJob.id == job_id)
        .with_for_update()
    )
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="PageSpeed scan job not found.",
        )
    if job.status != "failed":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only failed PageSpeed scan jobs can be retried.",
        )
    if not _job_has_pagespeed_key(job, db):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MISSING_PAGESPEED_KEY,
        )

    job.status = "queued"
    job.attempts = 0
    job.error = None
    job.started_at = None
    job.completed_at = None
    db.commit()
    await enqueue_pagespeed_jobs([job.id])
    return serialize_pagespeed_scan_job(job)


async def retry_failed_pagespeed_scan_jobs(
    job_ids: list[UUID],
    db: Session,
) -> BatchJobRetryResponse:
    unique_job_ids = list(dict.fromkeys(job_ids))
    jobs = db.scalars(
        select(PageSpeedScanJob)
        .where(PageSpeedScanJob.id.in_(unique_job_ids))
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
                    message="PageSpeed scan job not found.",
                )
            )
        elif job.status != "failed":
            failures.append(
                BatchJobRetryFailure(
                    job_id=job_id,
                    message="Only failed PageSpeed scan jobs can be retried.",
                )
            )
        elif not _job_has_pagespeed_key(job, db):
            failures.append(
                BatchJobRetryFailure(
                    job_id=job_id,
                    message=MISSING_PAGESPEED_KEY,
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
    await enqueue_pagespeed_jobs(retried_job_ids)
    return BatchJobRetryResponse(
        retried_job_ids=retried_job_ids,
        failures=failures,
    )


def list_pagespeed_scan_jobs(
    db: Session,
    limit: int = 100,
) -> list[PageSpeedScanJobResponse]:
    jobs = db.scalars(
        select(PageSpeedScanJob)
        .options(selectinload(PageSpeedScanJob.companies))
        .order_by(PageSpeedScanJob.created_at.desc(), PageSpeedScanJob.id.desc())
        .limit(limit)
    ).all()
    return [serialize_pagespeed_scan_job(job) for job in jobs]


def summarize_pagespeed_scan_jobs(db: Session) -> JobQueueStatusSummary:
    counts = dict(
        db.execute(
            select(PageSpeedScanJob.status, func.count())
            .where(PageSpeedScanJob.status.in_(("running", "failed")))
            .group_by(PageSpeedScanJob.status)
        ).all()
    )
    return JobQueueStatusSummary(
        running_count=counts.get("running", 0),
        failed_count=counts.get("failed", 0),
    )
