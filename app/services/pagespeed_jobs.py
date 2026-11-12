import logging
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import PageSpeedScanJob
from app.schemas.pagespeed import PageSpeedScanJobResponse
from app.services.redis_queue import (
    RedisQueueError,
    enqueue_pagespeed_jobs as push_pagespeed_jobs,
)


logger = logging.getLogger(__name__)


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

    job.status = "queued"
    job.attempts = 0
    job.error = None
    job.started_at = None
    job.completed_at = None
    db.commit()
    await enqueue_pagespeed_jobs([job.id])
    return serialize_pagespeed_scan_job(job)


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
