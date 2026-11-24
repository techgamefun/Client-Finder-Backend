import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import delete, exists, select, update
from starlette.concurrency import run_in_threadpool

from app.config import settings
from app.database import SessionLocal
from app.http_clients import (
    HTTPClientPool,
    current_http_client_pool,
    install_http_client_pool,
    reset_http_client_pool,
)
from app.models import (
    PageSpeedScan,
    PageSpeedScanJob,
    UserProviderCredential,
    Website,
    WebsiteAhrefsDomainRatingJob,
    WebsiteCrawl,
    WebsiteCrawlJob,
    WebsiteCorpusAnalysis,
    WebsiteCorpusAnalysisJob,
    WebsiteHypeStatFetchJob,
)
from app.services.ahrefs import fetch_website_domain_rating
from app.services.application_settings import (
    CrawlSettings,
    get_crawl_settings,
    get_user_provider_api_key,
)
from app.tenant_context import (
    TenantContext,
    reset_tenant_context,
    set_tenant_context,
)
from app.services.campaign_monitor import process_due_gmail_monitoring
from app.services.campaign_worker import process_due_campaign_recipient
from app.services.redis_queue import (
    RedisQueueError,
    dequeue_ahrefs_domain_rating_job,
    dequeue_corpus_analysis_job,
    dequeue_hypestat_fetch_job,
    dequeue_job,
    dequeue_pagespeed_job,
    enqueue_job,
    validate_redis_configuration,
)
from app.services.excluded_domains import (
    AUTO_RETRYABLE_FAILURE_CODES,
    find_excluded_domain,
    record_unreachable_domain_failure,
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
from app.services.website_ahrefs_jobs import (
    create_website_ahrefs_domain_rating_job,
    enqueue_website_ahrefs_domain_rating_jobs,
)
from app.services.website_hypestat import refetch_website_hypestat_profile
from app.services.website_corpus_jobs import (
    create_website_corpus_analysis_job,
    enqueue_website_corpus_analysis_jobs,
)
from app.services.website_corpus_words import (
    _ANALYSIS_VERSION,
    _analyze_sites,
    _same_timestamp,
    _site_text_record,
)
from app.services.normalization import normalize_domain
from app.schemas.website import WebsiteCorpusWordStat


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


def _save_success(
    job_id: UUID,
    crawl_data: CrawlData,
) -> tuple[list[UUID], list[UUID], list[UUID]]:
    with SessionLocal() as db:
        crawl = _persist_crawl(crawl_data, db)
        job = db.get(WebsiteCrawlJob, job_id)
        if job is None:
            raise RuntimeError(f"Website crawl job {job_id} no longer exists.")
        pagespeed_job_ids = create_website_pagespeed_jobs(
            crawl_data.website_url,
            db,
        )
        website = db.scalar(
            select(Website).where(Website.crawl_id == crawl.id)
        )
        if website is None:
            raise RuntimeError(
                "The website registry record was not created by the crawl."
            )
        corpus_job = create_website_corpus_analysis_job(
            website.id,
            crawl.crawled_at,
            db,
        )
        ahrefs_job = create_website_ahrefs_domain_rating_job(
            website.id,
            crawl.crawled_at,
            db,
        )
        now = datetime.now(timezone.utc)
        job.status = "succeeded"
        job.crawl_id = crawl.id
        job.error = None
        job.updated_at = now
        job.completed_at = now
        db.commit()
        return (
            pagespeed_job_ids,
            [corpus_job.id] if corpus_job is not None else [],
            [ahrefs_job.id] if ahrefs_job is not None else [],
        )


def _get_crawl_settings() -> CrawlSettings:
    with SessionLocal() as db:
        return get_crawl_settings(db)


def _claim_corpus_analysis_job(
    job_id: UUID,
) -> tuple[dict[str, object], TenantContext] | None:
    started_at = datetime.now(timezone.utc)
    with SessionLocal() as db:
        result = db.execute(
            update(WebsiteCorpusAnalysisJob)
            .where(
                WebsiteCorpusAnalysisJob.id == job_id,
                WebsiteCorpusAnalysisJob.status == "queued",
            )
            .values(
                status="running",
                attempts=WebsiteCorpusAnalysisJob.attempts + 1,
                started_at=started_at,
                updated_at=started_at,
                error=None,
            )
        )
        if result.rowcount != 1:
            db.rollback()
            return None

        job = db.get(WebsiteCorpusAnalysisJob, job_id)
        if job is None:
            db.rollback()
            return None
        site = db.execute(
            select(
                Website.domain,
                WebsiteCrawl.classification_signals,
                WebsiteCrawl.crawled_at,
            )
            .join(WebsiteCrawl, Website.crawl_id == WebsiteCrawl.id)
            .where(Website.id == job.website_id)
        ).first()
        if site is None or not _same_timestamp(
            job.source_crawled_at,
            site.crawled_at,
        ):
            now = datetime.now(timezone.utc)
            job.status = "skipped"
            job.error = "A newer crawl replaced this keyword generation job."
            job.updated_at = now
            job.completed_at = now
            db.commit()
            return None

        job_data = (
            _site_text_record(site.domain, site.classification_signals),
            TenantContext(
                organization_id=job.organization_id,
                user_id=job.created_by_user_id,
                email="",
                role="system",
            ),
        )
        db.commit()
        return job_data


def _find_recoverable_corpus_analysis_job() -> UUID | None:
    now = datetime.now(timezone.utc)
    lease_cutoff = now - timedelta(seconds=JOB_LEASE_SECONDS)
    with SessionLocal() as db:
        db.execute(
            update(WebsiteCorpusAnalysisJob)
            .where(
                WebsiteCorpusAnalysisJob.status == "running",
                WebsiteCorpusAnalysisJob.updated_at < lease_cutoff,
                WebsiteCorpusAnalysisJob.attempts >= MAX_JOB_ATTEMPTS,
            )
            .values(
                status="failed",
                error=(
                    "The worker stopped before keyword generation could finish."
                ),
                completed_at=now,
                updated_at=now,
            )
        )
        db.execute(
            update(WebsiteCorpusAnalysisJob)
            .where(
                WebsiteCorpusAnalysisJob.status == "running",
                WebsiteCorpusAnalysisJob.updated_at < lease_cutoff,
                WebsiteCorpusAnalysisJob.attempts < MAX_JOB_ATTEMPTS,
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
            select(WebsiteCorpusAnalysisJob.id)
            .where(
                WebsiteCorpusAnalysisJob.status == "queued",
                WebsiteCorpusAnalysisJob.created_at < recovery_cutoff,
            )
            .order_by(WebsiteCorpusAnalysisJob.created_at)
            .limit(1)
        )


def _claim_ahrefs_domain_rating_job(
    job_id: UUID,
) -> tuple[UUID, TenantContext] | None:
    started_at = datetime.now(timezone.utc)
    with SessionLocal() as db:
        job = db.get(WebsiteAhrefsDomainRatingJob, job_id)
        if job is None or job.created_by_user_id is None:
            db.rollback()
            return None
        api_key = get_user_provider_api_key(
            db,
            "ahrefs",
            user_id=job.created_by_user_id,
        )
        if not api_key:
            db.rollback()
            return None

        result = db.execute(
            update(WebsiteAhrefsDomainRatingJob)
            .where(
                WebsiteAhrefsDomainRatingJob.id == job_id,
                WebsiteAhrefsDomainRatingJob.status == "queued",
            )
            .values(
                status="running",
                attempts=WebsiteAhrefsDomainRatingJob.attempts + 1,
                started_at=started_at,
                updated_at=started_at,
                error=None,
            )
        )
        if result.rowcount != 1:
            db.rollback()
            return None

        job_data = (
            job.website_id,
            TenantContext(
                organization_id=job.organization_id,
                user_id=job.created_by_user_id,
                email="",
                role="system",
            ),
        )
        db.commit()
        return job_data


def _claim_hypestat_fetch_job(
    job_id: UUID,
) -> tuple[UUID, TenantContext] | None:
    started_at = datetime.now(timezone.utc)
    with SessionLocal() as db:
        job = db.get(WebsiteHypeStatFetchJob, job_id)
        if job is None or job.created_by_user_id is None:
            db.rollback()
            return None
        api_key = get_user_provider_api_key(
            db,
            "hypestat",
            user_id=job.created_by_user_id,
        )
        if not api_key:
            db.rollback()
            return None

        result = db.execute(
            update(WebsiteHypeStatFetchJob)
            .where(
                WebsiteHypeStatFetchJob.id == job_id,
                WebsiteHypeStatFetchJob.status == "queued",
            )
            .values(
                status="running",
                attempts=WebsiteHypeStatFetchJob.attempts + 1,
                started_at=started_at,
                updated_at=started_at,
                error=None,
            )
        )
        if result.rowcount != 1:
            db.rollback()
            return None

        job_data = (
            job.website_id,
            TenantContext(
                organization_id=job.organization_id,
                user_id=job.created_by_user_id,
                email="",
                role="system",
            ),
        )
        db.commit()
        return job_data


def _find_recoverable_hypestat_fetch_job() -> UUID | None:
    now = datetime.now(timezone.utc)
    lease_cutoff = now - timedelta(seconds=JOB_LEASE_SECONDS)
    with SessionLocal() as db:
        db.execute(
            update(WebsiteHypeStatFetchJob)
            .where(
                WebsiteHypeStatFetchJob.status == "running",
                WebsiteHypeStatFetchJob.updated_at < lease_cutoff,
                WebsiteHypeStatFetchJob.attempts >= MAX_JOB_ATTEMPTS,
            )
            .values(
                status="failed",
                error="The worker stopped before HypeStat data could be fetched.",
                completed_at=now,
                updated_at=now,
            )
        )
        db.execute(
            update(WebsiteHypeStatFetchJob)
            .where(
                WebsiteHypeStatFetchJob.status == "running",
                WebsiteHypeStatFetchJob.updated_at < lease_cutoff,
                WebsiteHypeStatFetchJob.attempts < MAX_JOB_ATTEMPTS,
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
            select(WebsiteHypeStatFetchJob.id)
            .where(
                WebsiteHypeStatFetchJob.status == "queued",
                WebsiteHypeStatFetchJob.created_at < recovery_cutoff,
                exists(
                    select(UserProviderCredential.id).where(
                        UserProviderCredential.user_id
                        == WebsiteHypeStatFetchJob.created_by_user_id,
                        UserProviderCredential.provider == "hypestat",
                    )
                ),
            )
            .order_by(WebsiteHypeStatFetchJob.created_at)
            .limit(1)
        )


def _find_recoverable_ahrefs_domain_rating_job() -> UUID | None:
    now = datetime.now(timezone.utc)
    lease_cutoff = now - timedelta(seconds=JOB_LEASE_SECONDS)
    with SessionLocal() as db:
        db.execute(
            update(WebsiteAhrefsDomainRatingJob)
            .where(
                WebsiteAhrefsDomainRatingJob.status == "running",
                WebsiteAhrefsDomainRatingJob.updated_at < lease_cutoff,
                WebsiteAhrefsDomainRatingJob.attempts >= MAX_JOB_ATTEMPTS,
            )
            .values(
                status="failed",
                error=(
                    "The worker stopped before Ahrefs Domain Rating "
                    "could be fetched."
                ),
                completed_at=now,
                updated_at=now,
            )
        )
        db.execute(
            update(WebsiteAhrefsDomainRatingJob)
            .where(
                WebsiteAhrefsDomainRatingJob.status == "running",
                WebsiteAhrefsDomainRatingJob.updated_at < lease_cutoff,
                WebsiteAhrefsDomainRatingJob.attempts < MAX_JOB_ATTEMPTS,
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
            select(WebsiteAhrefsDomainRatingJob.id)
            .where(
                WebsiteAhrefsDomainRatingJob.status == "queued",
                WebsiteAhrefsDomainRatingJob.created_at < recovery_cutoff,
                exists(
                    select(UserProviderCredential.id).where(
                        UserProviderCredential.user_id
                        == WebsiteAhrefsDomainRatingJob.created_by_user_id,
                        UserProviderCredential.provider == "ahrefs",
                    )
                ),
            )
            .order_by(WebsiteAhrefsDomainRatingJob.created_at)
            .limit(1)
        )


def _save_ahrefs_domain_rating_success(job_id: UUID) -> None:
    with SessionLocal() as db:
        job = db.get(WebsiteAhrefsDomainRatingJob, job_id)
        if job is None:
            raise RuntimeError(
                f"Ahrefs Domain Rating job {job_id} no longer exists."
            )
        now = datetime.now(timezone.utc)
        job.status = "succeeded"
        job.error = None
        job.updated_at = now
        job.completed_at = now
        db.commit()


def _save_ahrefs_domain_rating_failure(job_id: UUID, detail: str) -> None:
    with SessionLocal() as db:
        job = db.get(WebsiteAhrefsDomainRatingJob, job_id)
        if job is None:
            raise RuntimeError(
                f"Ahrefs Domain Rating job {job_id} no longer exists."
            )
        now = datetime.now(timezone.utc)
        job.status = "failed"
        job.error = detail
        job.updated_at = now
        job.completed_at = now
        db.commit()


def _save_hypestat_fetch_result(
    job_id: UUID,
    profile_status: str,
    detail: str | None,
) -> None:
    with SessionLocal() as db:
        job = db.get(WebsiteHypeStatFetchJob, job_id)
        if job is None:
            raise RuntimeError(f"HypeStat fetch job {job_id} no longer exists.")
        now = datetime.now(timezone.utc)
        if profile_status in ("succeeded", "queued", "invalid_domain", "removed"):
            job.status = "succeeded"
            job.error = None
        else:
            job.status = "failed"
            job.error = detail or "HypeStat lookup failed."
        job.updated_at = now
        job.completed_at = now
        db.commit()


def _save_hypestat_fetch_failure(job_id: UUID, detail: str) -> None:
    with SessionLocal() as db:
        job = db.get(WebsiteHypeStatFetchJob, job_id)
        if job is None:
            raise RuntimeError(f"HypeStat fetch job {job_id} no longer exists.")
        now = datetime.now(timezone.utc)
        job.status = "failed"
        job.error = detail
        job.updated_at = now
        job.completed_at = now
        db.commit()


def _save_corpus_analysis_success(
    job_id: UUID,
    words: list[WebsiteCorpusWordStat],
) -> None:
    with SessionLocal() as db:
        job = db.get(WebsiteCorpusAnalysisJob, job_id)
        if job is None:
            raise RuntimeError(
                f"Website keyword generation job {job_id} no longer exists."
            )
        website = db.get(Website, job.website_id)
        if website is None or website.crawl is None or not _same_timestamp(
            job.source_crawled_at,
            website.crawl.crawled_at,
        ):
            now = datetime.now(timezone.utc)
            job.status = "skipped"
            job.error = "A newer crawl replaced this keyword generation job."
            job.updated_at = now
            job.completed_at = now
            db.commit()
            return

        analysis = db.get(WebsiteCorpusAnalysis, website.id)
        if analysis is None:
            analysis = WebsiteCorpusAnalysis(website_id=website.id)
            db.add(analysis)
        analysis.words = [word.model_dump() for word in words]
        analysis.algorithm_version = _ANALYSIS_VERSION
        analysis.source_crawled_at = job.source_crawled_at
        now = datetime.now(timezone.utc)
        analysis.computed_at = now
        job.status = "succeeded"
        job.error = None
        job.updated_at = now
        job.completed_at = now
        db.commit()


def _save_corpus_analysis_failure(job_id: UUID, detail: str) -> None:
    with SessionLocal() as db:
        job = db.get(WebsiteCorpusAnalysisJob, job_id)
        if job is None:
            raise RuntimeError(
                f"Website keyword generation job {job_id} no longer exists."
            )
        now = datetime.now(timezone.utc)
        job.status = "failed"
        job.error = detail
        job.updated_at = now
        job.completed_at = now
        db.commit()


async def process_ahrefs_domain_rating_job(job_id: UUID) -> None:
    job_data = await run_in_threadpool(
        _claim_ahrefs_domain_rating_job,
        job_id,
    )
    if job_data is None:
        return
    website_id, context = job_data
    tenant_token = set_tenant_context(context)
    try:
        with SessionLocal() as db:
            await fetch_website_domain_rating(website_id, db)
        await run_in_threadpool(
            _save_ahrefs_domain_rating_success,
            job_id,
        )
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
        await run_in_threadpool(
            _save_ahrefs_domain_rating_failure,
            job_id,
            detail,
        )
    except Exception:
        logger.exception(
            "Ahrefs Domain Rating job %s failed unexpectedly.",
            job_id,
        )
        await run_in_threadpool(
            _save_ahrefs_domain_rating_failure,
            job_id,
            "Ahrefs Domain Rating fetch failed unexpectedly. Please retry.",
        )
    finally:
        reset_tenant_context(tenant_token)


async def process_hypestat_fetch_job(job_id: UUID) -> None:
    job_data = await run_in_threadpool(_claim_hypestat_fetch_job, job_id)
    if job_data is None:
        return
    website_id, context = job_data
    tenant_token = set_tenant_context(context)
    try:
        with SessionLocal() as db:
            profile = await refetch_website_hypestat_profile(website_id, db)
        await run_in_threadpool(
            _save_hypestat_fetch_result,
            job_id,
            profile.status,
            profile.error,
        )
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
        await run_in_threadpool(
            _save_hypestat_fetch_failure,
            job_id,
            detail,
        )
    except Exception:
        logger.exception("HypeStat fetch job %s failed unexpectedly.", job_id)
        await run_in_threadpool(
            _save_hypestat_fetch_failure,
            job_id,
            "HypeStat fetch failed unexpectedly. Please retry.",
        )
    finally:
        reset_tenant_context(tenant_token)


async def process_corpus_analysis_job(job_id: UUID) -> None:
    job_data = await run_in_threadpool(_claim_corpus_analysis_job, job_id)
    if job_data is None:
        return
    site_record, context = job_data
    tenant_token = set_tenant_context(context)
    try:
        domain = normalize_domain(str(site_record["domain"]))
        if domain is None:
            raise RuntimeError("The website has an invalid domain.")
        words_by_domain = await run_in_threadpool(_analyze_sites, [site_record])
        words = words_by_domain.get(domain)
        if words is None:
            raise RuntimeError(
                "The crawler service omitted a website corpus result."
            )
        await run_in_threadpool(
            _save_corpus_analysis_success,
            job_id,
            words,
        )
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
        await run_in_threadpool(
            _save_corpus_analysis_failure,
            job_id,
            detail,
        )
    except Exception:
        logger.exception(
            "Website keyword generation job %s failed unexpectedly.",
            job_id,
        )
        await run_in_threadpool(
            _save_corpus_analysis_failure,
            job_id,
            "Website keyword generation failed unexpectedly. Please retry.",
        )
    finally:
        reset_tenant_context(tenant_token)


def _save_failure(job_id: UUID, detail: str) -> None:
    with SessionLocal() as db:
        job = db.get(WebsiteCrawlJob, job_id)
        if job is None:
            raise RuntimeError(f"Website crawl job {job_id} no longer exists.")
        if job.status != "running":
            db.rollback()
            return
        now = datetime.now(timezone.utc)
        job.status = "failed"
        job.error = detail
        job.updated_at = now
        job.completed_at = now
        db.commit()


def _delete_excluded_job(job_id: UUID) -> None:
    with SessionLocal() as db:
        db.execute(
            delete(WebsiteCrawlJob).where(
                WebsiteCrawlJob.id == job_id,
                WebsiteCrawlJob.status == "running",
            )
        )
        db.commit()


def _excluded_reason(website_url: str) -> str | None:
    with SessionLocal() as db:
        exclusion = find_excluded_domain(db, website_url)
        return exclusion.reason if exclusion is not None else None


def _schedule_crawl_retry(job_id: UUID, detail: str) -> bool:
    with SessionLocal() as db:
        job = db.get(WebsiteCrawlJob, job_id)
        if job is None:
            raise RuntimeError(f"Website crawl job {job_id} no longer exists.")
        if job.status != "running" or job.attempts >= MAX_JOB_ATTEMPTS:
            db.rollback()
            return False
        now = datetime.now(timezone.utc)
        job.status = "queued"
        job.error = detail
        job.started_at = None
        job.completed_at = None
        job.updated_at = now
        db.commit()
        return True


def _record_unreachable_failure(website_url: str, detail: str) -> None:
    with SessionLocal() as db:
        record_unreachable_domain_failure(db, website_url, detail)


async def process_job(job_id: UUID) -> None:
    job_data = await run_in_threadpool(_claim_job, job_id)
    if job_data is None:
        return
    website_url, context = job_data
    tenant_token = set_tenant_context(context)

    try:
        exclusion_reason = await run_in_threadpool(
            _excluded_reason,
            website_url,
        )
        if exclusion_reason is not None:
            await run_in_threadpool(_delete_excluded_job, job_id)
            return

        crawl_settings = await run_in_threadpool(_get_crawl_settings)
        crawl_data = await crawl_website(
            website_url,
            max_pages=crawl_settings.max_pages,
            max_depth=crawl_settings.max_depth,
        )
        (
            pagespeed_job_ids,
            corpus_job_ids,
            ahrefs_job_ids,
        ) = await run_in_threadpool(
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
        await enqueue_website_corpus_analysis_jobs(corpus_job_ids)
        await enqueue_website_ahrefs_domain_rating_jobs(ahrefs_job_ids)
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
        failure_code = (exc.headers or {}).get("X-Crawler-Failure-Code")
        if failure_code in AUTO_RETRYABLE_FAILURE_CODES:
            retry_scheduled = await run_in_threadpool(
                _schedule_crawl_retry,
                job_id,
                detail,
            )
            if retry_scheduled:
                try:
                    await enqueue_job(str(job_id))
                except RedisQueueError:
                    logger.exception(
                        "Could not dispatch retry for website crawl job %s; "
                        "it remains queued for worker recovery.",
                        job_id,
                    )
                return

        await run_in_threadpool(_save_failure, job_id, detail)
        if failure_code in AUTO_RETRYABLE_FAILURE_CODES:
            await run_in_threadpool(
                _record_unreachable_failure,
                website_url,
                detail,
            )
    except Exception:
        logger.exception("Website crawl job %s failed unexpectedly.", job_id)
        await run_in_threadpool(
            _save_failure,
            job_id,
            "The website crawler failed unexpectedly. Please retry the job.",
        )
    finally:
        reset_tenant_context(tenant_token)


async def _process_queued_crawl_job(job_id: UUID) -> None:
    try:
        await process_job(job_id)
    except Exception:
        logger.exception("Could not process crawl job %s.", job_id)


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


async def _run_worker_loop() -> None:
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
    if not settings.python_worker_website_jobs_enabled:
        logger.info(
            "Python worker website queues are disabled; the Go background "
            "worker must be running.",
        )
    if (
        not settings.python_worker_campaign_sending_enabled
        or not settings.python_worker_gmail_monitoring_enabled
    ):
        logger.info(
            "Some Python campaign or Gmail jobs are disabled; ensure the "
            "corresponding Go worker jobs are enabled.",
        )
    prefer_pagespeed = False
    prefer_campaign = False
    last_monitor_poll = 0.0
    active_crawl_tasks: set[asyncio.Task[None]] = set()

    try:
        while True:
            active_crawl_tasks = {
                task for task in active_crawl_tasks if not task.done()
            }
            if (
                settings.python_worker_gmail_monitoring_enabled
                and time.monotonic() - last_monitor_poll >= 10
            ):
                last_monitor_poll = time.monotonic()
                await process_due_gmail_monitoring()

            campaign_checked = False
            if settings.python_worker_campaign_sending_enabled and prefer_campaign:
                prefer_campaign = False
                campaign_checked = True
                if await process_due_campaign_recipient():
                    continue

            job_id: UUID | None = None
            job_type: str | None = None
            queue_readers = (
                (
                    (dequeue_corpus_analysis_job, "keywords"),
                    (dequeue_pagespeed_job, "pagespeed"),
                    (dequeue_ahrefs_domain_rating_job, "ahrefs"),
                    (dequeue_hypestat_fetch_job, "hypestat"),
                    (dequeue_job, "crawl"),
                )
                if prefer_pagespeed
                else (
                    (dequeue_job, "crawl"),
                    (dequeue_pagespeed_job, "pagespeed"),
                    (dequeue_corpus_analysis_job, "keywords"),
                    (dequeue_ahrefs_domain_rating_job, "ahrefs"),
                    (dequeue_hypestat_fetch_job, "hypestat"),
                )
            )
            if not settings.python_worker_website_jobs_enabled:
                queue_readers = tuple(
                    (dequeue, queue_type)
                    for dequeue, queue_type in queue_readers
                    if queue_type not in {
                        "crawl",
                        "pagespeed",
                        "keywords",
                        "ahrefs",
                        "hypestat",
                    }
                )
            prefer_pagespeed = not prefer_pagespeed
            for dequeue, queue_type in (
                queue_readers if redis_enabled else ()
            ):
                if (
                    queue_type == "crawl"
                    and len(active_crawl_tasks)
                    >= settings.crawl_job_concurrency
                ):
                    continue
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
                        (_find_recoverable_corpus_analysis_job, "keywords"),
                        (_find_recoverable_pagespeed_job, "pagespeed"),
                        (_find_recoverable_ahrefs_domain_rating_job, "ahrefs"),
                        (_find_recoverable_hypestat_fetch_job, "hypestat"),
                        (_find_recoverable_job, "crawl"),
                    )
                    if prefer_pagespeed
                    else (
                        (_find_recoverable_job, "crawl"),
                        (_find_recoverable_pagespeed_job, "pagespeed"),
                        (_find_recoverable_corpus_analysis_job, "keywords"),
                        (_find_recoverable_ahrefs_domain_rating_job, "ahrefs"),
                        (_find_recoverable_hypestat_fetch_job, "hypestat"),
                    )
                )
                if not settings.python_worker_website_jobs_enabled:
                    recovery_readers = tuple(
                        (recover, recovery_type)
                        for recover, recovery_type in recovery_readers
                        if recovery_type not in {
                            "crawl",
                            "pagespeed",
                            "keywords",
                            "ahrefs",
                            "hypestat",
                        }
                    )
                for recover, recovery_type in recovery_readers:
                    if (
                        recovery_type == "crawl"
                        and len(active_crawl_tasks)
                        >= settings.crawl_job_concurrency
                    ):
                        continue
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
                if (
                    settings.python_worker_campaign_sending_enabled
                    and not campaign_checked
                    and await process_due_campaign_recipient()
                ):
                    continue
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
                continue

            prefer_campaign = True
            if job_type == "crawl":
                active_crawl_tasks.add(
                    asyncio.create_task(_process_queued_crawl_job(job_id))
                )
                continue

            try:
                if job_type == "pagespeed":
                    await process_pagespeed_job(job_id)
                elif job_type == "keywords":
                    await process_corpus_analysis_job(job_id)
                elif job_type == "ahrefs":
                    await process_ahrefs_domain_rating_job(job_id)
                elif job_type == "hypestat":
                    await process_hypestat_fetch_job(job_id)
            except Exception:
                logger.exception("Could not process %s job %s.", job_type, job_id)
    finally:
        for task in active_crawl_tasks:
            task.cancel()
        if active_crawl_tasks:
            await asyncio.gather(*active_crawl_tasks, return_exceptions=True)


async def run_worker() -> None:
    if current_http_client_pool() is not None:
        await _run_worker_loop()
        return

    pool = HTTPClientPool()
    token = install_http_client_pool(pool)
    try:
        await _run_worker_loop()
    finally:
        reset_http_client_pool(token)
        await pool.aclose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_worker())
