import asyncio
import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select, update
from starlette.concurrency import run_in_threadpool

from app.database import SessionLocal
from app.models import WebsiteCrawlJob
from app.services.redis_queue import (
    RedisQueueError,
    dequeue_job,
    validate_redis_configuration,
)
from app.services.website_crawler import (
    CrawlData,
    _persist_crawl,
    crawl_website,
)


logger = logging.getLogger(__name__)
POLL_INTERVAL_SECONDS = 2
RECOVERY_GRACE_SECONDS = 30
JOB_LEASE_SECONDS = 300
MAX_JOB_ATTEMPTS = 3


def _claim_job(job_id: UUID) -> str | None:
    with SessionLocal() as db:
        result = db.execute(
            update(WebsiteCrawlJob)
            .where(
                WebsiteCrawlJob.id == job_id,
                WebsiteCrawlJob.status == "queued",
            )
            .values(
                status="running",
                attempts=WebsiteCrawlJob.attempts + 1,
                updated_at=datetime.now(timezone.utc),
                error=None,
            )
        )
        if result.rowcount != 1:
            db.rollback()
            return None

        job = db.get(WebsiteCrawlJob, job_id)
        if job is None:
            db.rollback()
            return None
        website_url = job.website_url
        db.commit()
        return website_url


def _find_recoverable_job() -> UUID | None:
    now = datetime.now(timezone.utc)
    lease_cutoff = now - timedelta(seconds=JOB_LEASE_SECONDS)
    with SessionLocal() as db:
        db.execute(
            update(WebsiteCrawlJob)
            .where(
                WebsiteCrawlJob.status == "running",
                WebsiteCrawlJob.updated_at < lease_cutoff,
                WebsiteCrawlJob.attempts >= MAX_JOB_ATTEMPTS,
            )
            .values(
                status="failed",
                error="The worker stopped before this crawl could finish.",
                completed_at=now,
                updated_at=now,
            )
        )
        db.execute(
            update(WebsiteCrawlJob)
            .where(
                WebsiteCrawlJob.status == "running",
                WebsiteCrawlJob.updated_at < lease_cutoff,
                WebsiteCrawlJob.attempts < MAX_JOB_ATTEMPTS,
            )
            .values(
                status="queued",
                error="Retrying after the worker stopped unexpectedly.",
                updated_at=now,
            )
        )
        db.commit()

        recovery_cutoff = now - timedelta(seconds=RECOVERY_GRACE_SECONDS)
        return db.scalar(
            select(WebsiteCrawlJob.id)
            .where(
                WebsiteCrawlJob.status == "queued",
                WebsiteCrawlJob.created_at < recovery_cutoff,
            )
            .order_by(WebsiteCrawlJob.created_at)
            .limit(1)
        )


def _save_success(job_id: UUID, crawl_data: CrawlData) -> None:
    with SessionLocal() as db:
        crawl = _persist_crawl(crawl_data, db)
        job = db.get(WebsiteCrawlJob, job_id)
        if job is None:
            raise RuntimeError(f"Website crawl job {job_id} no longer exists.")
        now = datetime.now(timezone.utc)
        job.status = "succeeded"
        job.crawl_id = crawl.id
        job.error = None
        job.updated_at = now
        job.completed_at = now
        db.commit()


def _save_failure(job_id: UUID, detail: str) -> None:
    with SessionLocal() as db:
        job = db.get(WebsiteCrawlJob, job_id)
        if job is None:
            raise RuntimeError(f"Website crawl job {job_id} no longer exists.")
        now = datetime.now(timezone.utc)
        job.status = "failed"
        job.error = detail
        job.updated_at = now
        job.completed_at = now
        db.commit()


async def process_job(job_id: UUID) -> None:
    website_url = await run_in_threadpool(_claim_job, job_id)
    if website_url is None:
        return

    try:
        crawl_data = await crawl_website(website_url)
        await run_in_threadpool(_save_success, job_id, crawl_data)
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
        await run_in_threadpool(_save_failure, job_id, detail)
    except Exception:
        logger.exception("Website crawl job %s failed unexpectedly.", job_id)
        await run_in_threadpool(
            _save_failure,
            job_id,
            "The website crawler failed unexpectedly. Please retry the job.",
        )


async def run_worker() -> None:
    validate_redis_configuration()
    logger.info("Website crawl worker started.")

    while True:
        try:
            queued_id = await dequeue_job()
        except RedisQueueError:
            logger.exception("Could not poll the Redis website crawl queue.")
            queued_id = None

        job_id: UUID | None = None
        if queued_id is not None:
            try:
                job_id = UUID(queued_id)
            except ValueError:
                logger.error("Discarding invalid website crawl job ID from Redis.")

        if job_id is None:
            try:
                job_id = await run_in_threadpool(_find_recoverable_job)
            except Exception:
                logger.exception("Could not recover pending website crawl jobs.")

        if job_id is None:
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
            continue

        try:
            await process_job(job_id)
        except Exception:
            logger.exception("Could not process website crawl job %s.", job_id)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_worker())
