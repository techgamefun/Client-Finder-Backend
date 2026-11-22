import logging
from datetime import datetime, timezone
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.models import Website, WebsiteCorpusAnalysisJob
from app.schemas.job_queue import BatchJobRetryFailure, BatchJobRetryResponse
from app.schemas.website import WebsiteCorpusAnalysisJobResponse
from app.services.redis_queue import (
    RedisQueueError,
    enqueue_corpus_analysis_jobs as push_corpus_analysis_jobs,
)


logger = logging.getLogger(__name__)


def create_website_corpus_analysis_job(
    website_id: UUID,
    source_crawled_at: datetime,
    db: Session,
) -> WebsiteCorpusAnalysisJob | None:
    existing = db.scalar(
        select(WebsiteCorpusAnalysisJob).where(
            WebsiteCorpusAnalysisJob.website_id == website_id,
            WebsiteCorpusAnalysisJob.source_crawled_at == source_crawled_at,
        )
    )
    if existing is not None:
        if existing.status != "succeeded":
            return None
        existing.status = "queued"
        existing.attempts = 0
        existing.error = None
        existing.started_at = None
        existing.completed_at = None
        existing.updated_at = datetime.now(timezone.utc)
        db.flush()
        return existing

    job = WebsiteCorpusAnalysisJob(
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


async def enqueue_website_corpus_analysis_jobs(
    job_ids: list[UUID],
) -> None:
    if not job_ids:
        return
    try:
        await push_corpus_analysis_jobs([str(job_id) for job_id in job_ids])
    except RedisQueueError:
        logger.exception(
            "Could not dispatch website keyword generation jobs; queued jobs "
            "will be picked up by worker recovery."
        )


def serialize_website_corpus_analysis_job(
    job: WebsiteCorpusAnalysisJob,
    website: Website,
) -> WebsiteCorpusAnalysisJobResponse:
    return WebsiteCorpusAnalysisJobResponse(
        id=job.id,
        website_id=website.id,
        website_url=website.website_url,
        domain=website.domain,
        source_crawled_at=job.source_crawled_at,
        status=job.status,
        attempts=job.attempts,
        error=job.error,
        created_at=job.created_at,
        updated_at=job.updated_at,
        started_at=job.started_at,
        completed_at=job.completed_at,
    )


def list_website_corpus_analysis_jobs(
    db: Session,
    limit: int = 100,
) -> list[WebsiteCorpusAnalysisJobResponse]:
    rows = db.execute(
        select(WebsiteCorpusAnalysisJob, Website)
        .join(Website, Website.id == WebsiteCorpusAnalysisJob.website_id)
        .order_by(
            WebsiteCorpusAnalysisJob.created_at.desc(),
            WebsiteCorpusAnalysisJob.id.desc(),
        )
        .limit(limit)
    ).all()
    return [
        serialize_website_corpus_analysis_job(job, website)
        for job, website in rows
    ]


async def retry_website_corpus_analysis_job(
    job_id: UUID,
    db: Session,
) -> WebsiteCorpusAnalysisJobResponse:
    job_id, response = await run_in_threadpool(
        _retry_website_corpus_analysis_job_sync,
        job_id,
        db,
    )
    await enqueue_website_corpus_analysis_jobs([job_id])
    return response


def _retry_website_corpus_analysis_job_sync(
    job_id: UUID,
    db: Session,
) -> tuple[UUID, WebsiteCorpusAnalysisJobResponse]:
    row = db.execute(
        select(WebsiteCorpusAnalysisJob, Website)
        .join(Website, Website.id == WebsiteCorpusAnalysisJob.website_id)
        .where(WebsiteCorpusAnalysisJob.id == job_id)
        .with_for_update()
    ).first()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Website keyword generation job not found.",
        )
    job, website = row
    if job.status != "failed":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only failed website keyword generation jobs can be retried.",
        )

    now = datetime.now(timezone.utc)
    job.status = "queued"
    job.attempts = 0
    job.error = None
    job.started_at = None
    job.completed_at = None
    job.updated_at = now
    db.commit()
    return job.id, serialize_website_corpus_analysis_job(job, website)


async def retry_failed_website_corpus_analysis_jobs(
    job_ids: list[UUID],
    db: Session,
) -> BatchJobRetryResponse:
    response, retried_job_ids = await run_in_threadpool(
        _retry_failed_website_corpus_analysis_jobs_sync,
        job_ids,
        db,
    )
    if retried_job_ids:
        await enqueue_website_corpus_analysis_jobs(retried_job_ids)
    return response


def _retry_failed_website_corpus_analysis_jobs_sync(
    job_ids: list[UUID],
    db: Session,
) -> tuple[BatchJobRetryResponse, list[UUID]]:
    unique_job_ids = list(dict.fromkeys(job_ids))
    jobs = db.scalars(
        select(WebsiteCorpusAnalysisJob)
        .where(WebsiteCorpusAnalysisJob.id.in_(unique_job_ids))
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
                    message="Website keyword generation job not found.",
                )
            )
        elif job.status != "failed":
            failures.append(
                BatchJobRetryFailure(
                    job_id=job_id,
                    message=(
                        "Only failed website keyword generation jobs can be retried."
                    ),
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
