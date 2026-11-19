import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import exists, select, update
from starlette.concurrency import run_in_threadpool

from app.database import SessionLocal
from app.models import (
    PageSpeedScan,
    PageSpeedScanJob,
    UserProviderCredential,
    WebsiteCrawlJob,
)
from app.services.application_settings import get_user_provider_api_key
from app.tenant_context import (
    TenantContext,
    reset_tenant_context,
    set_tenant_context,
)
from app.services.application_settings import CrawlSettings, get_crawl_settings
from app.services.campaign_worker import process_due_campaign_recipient
from app.services.campaign_monitor import process_due_gmail_monitoring
from app.services.redis_queue import (
    RedisQueueError,
    dequeue_job,
    dequeue_pagespeed_job,
    validate_redis_configuration,
)
from app.services.pagespeed import (
    PageSpeedError,
    PageSpeedScanData,
    scan_pagespeed,
)
from app.services.pagespeed_jobs import (
    create_website_pagespeed_jobs,
    enqueue_pagespeed_jobs,
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


def _claim_job(job_id: UUID) -> tuple[str, TenantContext] | None:
    started_at = datetime.now(timezone.utc)
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
                started_at=started_at,
                updated_at=started_at,
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
        job_data = (
            job.website_url,
            TenantContext(
                organization_id=job.organization_id,
                user_id=job.created_by_user_id,
                email="",
                role="system",
            ),
        )
        db.commit()
        return job_data


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
                started_at=None,
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


def _save_success(job_id: UUID, crawl_data: CrawlData) -> list[UUID]:
    with SessionLocal() as db:
        crawl = _persist_crawl(crawl_data, db)
        job = db.get(WebsiteCrawlJob, job_id)
        if job is None:
            raise RuntimeError(f"Website crawl job {job_id} no longer exists.")
        pagespeed_job_ids = create_website_pagespeed_jobs(
            crawl_data.website_url,
            db,
        )
        now = datetime.now(timezone.utc)
        job.status = "succeeded"
        job.crawl_id = crawl.id
        job.error = None
        job.updated_at = now
        job.completed_at = now
        db.commit()
        return pagespeed_job_ids


def _get_crawl_settings() -> CrawlSettings:
    with SessionLocal() as db:
        return get_crawl_settings(db)


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
    job_data = await run_in_threadpool(_claim_job, job_id)
    if job_data is None:
        return
    website_url, context = job_data
    tenant_token = set_tenant_context(context)

    try:
        crawl_settings = await run_in_threadpool(_get_crawl_settings)
        crawl_data = await crawl_website(
            website_url,
            max_pages=crawl_settings.max_pages,
            max_depth=crawl_settings.max_depth,
        )
        pagespeed_job_ids = await run_in_threadpool(
            _save_success,
            job_id,
            crawl_data,
        )
        try:
            await enqueue_pagespeed_jobs(pagespeed_job_ids)
        except Exception:
            logger.exception(
                "Could not dispatch PageSpeed scans after successful website "
                "crawl job %s; queued jobs remain available for recovery.",
                job_id,
            )
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
    finally:
        reset_tenant_context(tenant_token)


def _claim_pagespeed_job(
    job_id: UUID,
) -> tuple[list[UUID], UUID | None, str, str, TenantContext, str] | None:
    started_at = datetime.now(timezone.utc)
    with SessionLocal() as db:
        job = db.get(PageSpeedScanJob, job_id)
        if job is None or job.created_by_user_id is None:
            db.rollback()
            return None
        api_key = get_user_provider_api_key(
            db,
            "pagespeed",
            user_id=job.created_by_user_id,
        )
        if not api_key:
            db.rollback()
            return None

        result = db.execute(
            update(PageSpeedScanJob)
            .where(
                PageSpeedScanJob.id == job_id,
                PageSpeedScanJob.status == "queued",
            )
            .values(
                status="running",
                attempts=PageSpeedScanJob.attempts + 1,
                started_at=started_at,
                updated_at=started_at,
                error=None,
            )
        )
        if result.rowcount != 1:
            db.rollback()
            return None

        job_data = (
            [company.id for company in job.companies],
            job.website_id,
            job.website_url,
            job.strategy,
            TenantContext(
                organization_id=job.organization_id,
                user_id=job.created_by_user_id,
                email="",
                role="system",
            ),
            api_key,
        )
        db.commit()
        return job_data


def _find_recoverable_pagespeed_job() -> UUID | None:
    now = datetime.now(timezone.utc)
    lease_cutoff = now - timedelta(seconds=JOB_LEASE_SECONDS)
    with SessionLocal() as db:
        db.execute(
            update(PageSpeedScanJob)
            .where(
                PageSpeedScanJob.status == "running",
                PageSpeedScanJob.updated_at < lease_cutoff,
                PageSpeedScanJob.attempts >= MAX_JOB_ATTEMPTS,
            )
            .values(
                status="failed",
                error="The worker stopped before this scan could finish.",
                completed_at=now,
                updated_at=now,
            )
        )
        db.execute(
            update(PageSpeedScanJob)
            .where(
                PageSpeedScanJob.status == "running",
                PageSpeedScanJob.updated_at < lease_cutoff,
                PageSpeedScanJob.attempts < MAX_JOB_ATTEMPTS,
            )
            .values(
                status="queued",
                started_at=None,
                error="Retrying after the worker stopped unexpectedly.",
                updated_at=now,
            )
        )
        db.commit()

        recovery_cutoff = now - timedelta(seconds=RECOVERY_GRACE_SECONDS)
        return db.scalar(
            select(PageSpeedScanJob.id)
            .where(
                PageSpeedScanJob.status == "queued",
                PageSpeedScanJob.created_at < recovery_cutoff,
                exists(
                    select(UserProviderCredential.id).where(
                        UserProviderCredential.user_id
                        == PageSpeedScanJob.created_by_user_id,
                        UserProviderCredential.provider == "pagespeed",
                    )
                ),
            )
            .order_by(PageSpeedScanJob.created_at)
            .limit(1)
        )


def _save_pagespeed_success(
    job_id: UUID,
    company_ids: list[UUID],
    website_id: UUID | None,
    scan_data: PageSpeedScanData,
) -> None:
    with SessionLocal() as db:
        job = db.get(PageSpeedScanJob, job_id)
        if job is None:
            raise RuntimeError(f"PageSpeed scan job {job_id} no longer exists.")
        scans = [
            PageSpeedScan(
                company_id=company_id,
                website_id=website_id,
                website_url=scan_data.website_url,
                strategy=scan_data.strategy,
                category_scores=scan_data.category_scores,
                report=scan_data.report,
            )
            for company_id in company_ids
        ]
        if not scans and website_id is not None:
            scans.append(
                PageSpeedScan(
                    company_id=None,
                    website_id=website_id,
                    website_url=scan_data.website_url,
                    strategy=scan_data.strategy,
                    category_scores=scan_data.category_scores,
                    report=scan_data.report,
                )
            )
        db.add_all(scans)
        db.flush()

        now = datetime.now(timezone.utc)
        job.status = "succeeded"
        job.scan_id = scans[0].id if scans else None
        job.error = None
        job.updated_at = now
        job.completed_at = now
        db.commit()


def _save_pagespeed_failure(job_id: UUID, detail: str) -> None:
    with SessionLocal() as db:
        job = db.get(PageSpeedScanJob, job_id)
        if job is None:
            raise RuntimeError(f"PageSpeed scan job {job_id} no longer exists.")
        now = datetime.now(timezone.utc)
        job.status = "failed"
        job.error = detail
        job.updated_at = now
        job.completed_at = now
        db.commit()


async def process_pagespeed_job(job_id: UUID) -> None:
    job_data = await run_in_threadpool(_claim_pagespeed_job, job_id)
    if job_data is None:
        return

    company_ids, website_id, website_url, strategy, context, api_key = job_data
    tenant_token = set_tenant_context(context)
    try:
        if strategy not in ("mobile", "desktop"):
            raise PageSpeedError("The PageSpeed scan job has an invalid strategy.")
        scan_data = await scan_pagespeed(website_url, strategy, api_key)
        await run_in_threadpool(
            _save_pagespeed_success,
            job_id,
            company_ids,
            website_id,
            scan_data,
        )
    except PageSpeedError as exc:
        await run_in_threadpool(_save_pagespeed_failure, job_id, str(exc))
    except Exception:
        logger.exception("PageSpeed scan job %s failed unexpectedly.", job_id)
        await run_in_threadpool(
            _save_pagespeed_failure,
            job_id,
            "The PageSpeed scan failed unexpectedly. Please retry the job.",
        )
    finally:
        reset_tenant_context(tenant_token)


async def run_worker() -> None:
    try:
        validate_redis_configuration()
        redis_enabled = True
    except RedisQueueError as exc:
        logger.warning(
            "Redis is unavailable; using database job recovery: %s",
            exc,
        )
        redis_enabled = False

    logger.info("Background job worker started.")
    prefer_pagespeed = False
    prefer_campaign = False
    last_monitor_poll = 0.0

    while True:
        if time.monotonic() - last_monitor_poll >= 10:
            last_monitor_poll = time.monotonic()
            await process_due_gmail_monitoring()

        campaign_checked = False
        if prefer_campaign:
            prefer_campaign = False
            campaign_checked = True
            if await process_due_campaign_recipient():
                continue

        job_id: UUID | None = None
        job_type: str | None = None
        queue_readers = (
            (dequeue_pagespeed_job, "pagespeed"),
            (dequeue_job, "crawl"),
        ) if prefer_pagespeed else (
            (dequeue_job, "crawl"),
            (dequeue_pagespeed_job, "pagespeed"),
        )
        prefer_pagespeed = not prefer_pagespeed
        for dequeue, queue_type in (
            queue_readers if redis_enabled else ()
        ):
            try:
                queued_id = await dequeue()
            except RedisQueueError:
                logger.exception("Could not poll the Redis %s queue.", queue_type)
                redis_enabled = False
                break
            if queued_id is None:
                continue
            try:
                job_id = UUID(queued_id)
                job_type = queue_type
                break
            except ValueError:
                logger.error(
                    "Discarding invalid %s job ID from Redis.",
                    queue_type,
                )

        if job_id is None:
            recovery_readers = (
                (
                    (_find_recoverable_pagespeed_job, "pagespeed"),
                    (_find_recoverable_job, "crawl"),
                )
                if prefer_pagespeed
                else (
                    (_find_recoverable_job, "crawl"),
                    (_find_recoverable_pagespeed_job, "pagespeed"),
                )
            )
            for recover, recovery_type in recovery_readers:
                try:
                    job_id = await run_in_threadpool(recover)
                except Exception:
                    logger.exception(
                        "Could not recover pending %s jobs.",
                        recovery_type,
                    )
                    continue
                if job_id is not None:
                    job_type = recovery_type
                    break

        if job_id is None:
            if not campaign_checked and await process_due_campaign_recipient():
                continue
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
            continue

        prefer_campaign = True
        try:
            if job_type == "pagespeed":
                await process_pagespeed_job(job_id)
            else:
                await process_job(job_id)
        except Exception:
            logger.exception("Could not process %s job %s.", job_type, job_id)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_worker())
