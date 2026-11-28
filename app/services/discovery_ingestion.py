import asyncio
from collections.abc import Sequence
from datetime import datetime, timezone
from uuid import UUID
from urllib.parse import urlsplit, urlunsplit

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.models import (
    PageSpeedScanJob,
    SearchKeyword,
    Website,
    WebsiteCrawl,
)
from app.schemas.discovery import (
    DiscoveryCrawlFailure,
    DiscoveryIngestRequest,
    DiscoveryIngestResponse,
    DiscoveryResult,
)
from app.services.application_settings import get_crawl_settings
from app.services.discovery import search_websites
from app.services.contact_import import add_crawl_contacts
from app.services.normalization import normalize_domain
from app.services.pagespeed import PAGESPEED_STRATEGIES
from app.services.pagespeed_jobs import (
    enqueue_pagespeed_jobs,
    serialize_pagespeed_scan_job,
)
from app.services.website_crawler import CrawlData, crawl_website
from app.services.website_registry import canonical_website_url, upsert_website


def _persist_search_results(
    results: Sequence[DiscoveryResult],
    crawls_by_url: dict[str, CrawlData],
    pagespeed_targets_by_source: dict[str, str],
    db: Session,
    query: str,
    search_id: UUID | None,
) -> tuple[int, int, int, int, list[PageSpeedScanJob]]:
    for attempt in range(2):
        websites_created_ids: set[UUID] = set()
        skipped = 0
        contacts_added = 0
        enriched_website_ids: set[UUID] = set()
        pagespeed_jobs_by_key: dict[tuple[str, str], PageSpeedScanJob] = {}
        pagespeed_jobs: list[PageSpeedScanJob] = []
        seen_urls: set[str] = set()
        linked_website_ids: set[UUID] = set()

        try:
            search_keyword = (
                db.get(SearchKeyword, search_id)
                if search_id is not None
                else SearchKeyword(
                    keyword=query,
                    searched_at=datetime.now(timezone.utc),
                )
            )
            if search_keyword is None:
                raise RuntimeError("Discovery search record no longer exists.")
            if search_id is None:
                db.add(search_keyword)
                db.flush()

            for result in results:
                source_url = str(result.url)
                if source_url in seen_urls:
                    skipped += 1
                    continue
                seen_urls.add(source_url)

                source_domain = normalize_domain(source_url)
                existing_website_id = (
                    db.scalar(
                        select(Website.id)
                        .where(Website.domain == source_domain)
                        .limit(1)
                    )
                    if source_domain is not None
                    else None
                )
                crawl = crawls_by_url.get(source_url)
                crawl_record = (
                    _upsert_crawl(
                        crawl,
                        db,
                        discovery_source=result.provider.value,
                        discovery_provider=result.provider.value,
                    )
                    if crawl is not None
                    else None
                )
                website = upsert_website(
                    db,
                    source_url,
                    discovery_source=result.provider.value,
                    discovery_provider=result.provider.value,
                    category=crawl.site_type if crawl is not None else None,
                    crawl_id=(
                        crawl_record.id
                        if crawl_record is not None
                        and crawl_record.website_url == source_url
                        else None
                    ),
                )

                candidate_name = result.title.strip()[:200]
                if not candidate_name:
                    skipped += 1
                    continue

                if existing_website_id is None:
                    websites_created_ids.add(website.id)
                if not website.business_name:
                    website.business_name = candidate_name
                    enriched_website_ids.add(website.id)
                if website.status == "new":
                    website.status = "needs_review"

                if website.id not in linked_website_ids:
                    if all(
                        keyword.id != search_keyword.id
                        for keyword in website.search_keywords
                    ):
                        website.search_keywords.append(search_keyword)
                    linked_website_ids.add(website.id)

                is_listing = website.page_type == "business_listing"
                if crawl is not None and not is_listing:
                    added_contacts = add_crawl_contacts(
                        website,
                        crawl.emails,
                        crawl.phones,
                    )
                    if added_contacts:
                        contacts_added += added_contacts
                        enriched_website_ids.add(website.id)

                if not is_listing:
                    website_url = pagespeed_targets_by_source.get(
                        source_url,
                        _homepage_url(source_url),
                    )
                    for strategy in PAGESPEED_STRATEGIES:
                        job_key = (str(website.id), strategy)
                        job = pagespeed_jobs_by_key.get(job_key)
                        if job is None:
                            job = PageSpeedScanJob(
                                website_url=website_url,
                                strategy=strategy,
                                website_id=website.id,
                            )
                            db.add(job)
                            pagespeed_jobs_by_key[job_key] = job
                            pagespeed_jobs.append(job)

            db.commit()
            return (
                len(websites_created_ids),
                skipped,
                len(enriched_website_ids),
                contacts_added,
                pagespeed_jobs,
            )
        except IntegrityError:
            db.rollback()
            if attempt == 1:
                raise
        except Exception:
            db.rollback()
            raise

    raise RuntimeError("Could not persist search results after retry.")


def _homepage_url(url: str) -> str:
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc, "/", "", ""))


def _upsert_crawl(
    crawl: CrawlData,
    db: Session,
    *,
    discovery_source: str = "manual",
    discovery_provider: str | None = None,
) -> WebsiteCrawl:
    record = db.scalar(
        select(WebsiteCrawl)
        .where(WebsiteCrawl.website_url == crawl.website_url)
        .limit(1)
    )
    if record is None:
        record = WebsiteCrawl(website_url=crawl.website_url)
        db.add(record)

    if crawl.site_type is not None:
        record.site_type = crawl.site_type
    page_signals = [
        page.model_dump(mode="json")
        for page in crawl.pages
        if page.classification_signals is not None
    ]
    if page_signals:
        record.classification_signals = page_signals
    record.about_page_url = crawl.about_page_url
    record.about_content = crawl.about_content
    record.contact_page_url = crawl.contact_page_url
    record.contact_content = crawl.contact_content
    record.goals_page_url = crawl.goals_page_url
    record.goals_content = crawl.goals_content
    record.social_links = crawl.social_links
    record.crawled_at = datetime.now(timezone.utc)
    db.flush()
    upsert_website(
        db,
        record.website_url,
        discovery_source=discovery_source,
        discovery_provider=discovery_provider,
        category=record.site_type,
        crawl_id=record.id,
        language=crawl.language,
    )
    return record


def _pagespeed_target_urls(
    results: Sequence[DiscoveryResult],
) -> dict[str, str]:
    return {
        str(result.url): canonical_website_url(str(result.url))
        for result in results
    }


async def ingest_search_results(
    request: DiscoveryIngestRequest,
    db: Session,
) -> DiscoveryIngestResponse:
    if request.search_id is not None:
        search_keyword = await run_in_threadpool(
            db.get,
            SearchKeyword,
            request.search_id,
        )
        if search_keyword is None or search_keyword.keyword != request.query:
            raise HTTPException(
                status_code=422,
                detail="search_id does not match a recorded search keyword.",
            )

    if request.results is None:
        search_response = await search_websites(
            query=request.query,
            db=db,
            max_results=request.max_results,
        )
        results = search_response.results
    else:
        results = request.results

    semaphore = asyncio.Semaphore(10)
    crawl_settings = await run_in_threadpool(get_crawl_settings, db)
    crawl_failures: list[DiscoveryCrawlFailure] = []
    crawls_by_url: dict[str, CrawlData] = {}

    async def crawl_result(
        result: DiscoveryResult,
    ) -> tuple[str, CrawlData | None, DiscoveryCrawlFailure | None]:
        source_url = str(result.url)
        async with semaphore:
            try:
                return (
                    source_url,
                    await crawl_website(
                        source_url,
                        max_pages=crawl_settings.max_pages,
                        max_depth=crawl_settings.max_depth,
                    ),
                    None,
                )
            except HTTPException as exc:
                return (
                    source_url,
                    None,
                    DiscoveryCrawlFailure(
                        source_url=source_url,
                        detail=str(exc.detail),
                    ),
                )

    unique_results: list[DiscoveryResult] = []
    seen_urls: set[str] = set()
    for result in results:
        source_url = str(result.url)
        if source_url not in seen_urls:
            unique_results.append(result)
            seen_urls.add(source_url)

    crawl_results_task = asyncio.gather(
        *(crawl_result(result) for result in unique_results)
    )

    pagespeed_targets_by_source = await run_in_threadpool(
        _pagespeed_target_urls,
        unique_results,
    )
    crawl_results = await crawl_results_task
    for source_url, crawl, failure in crawl_results:
        if crawl is not None:
            crawls_by_url[source_url] = crawl
        if failure is not None:
            crawl_failures.append(failure)

    (
        websites_created,
        skipped,
        websites_enriched,
        contacts_added,
        pagespeed_jobs,
    ) = await run_in_threadpool(
        _persist_search_results,
        results,
        crawls_by_url,
        pagespeed_targets_by_source,
        db,
        request.query,
        request.search_id,
    )

    await enqueue_pagespeed_jobs([job.id for job in pagespeed_jobs])

    return DiscoveryIngestResponse(
        query=request.query,
        results_found=len(results),
        websites_created=websites_created,
        results_skipped=skipped,
        websites_crawled=len(crawls_by_url),
        websites_enriched=websites_enriched,
        contacts_added=contacts_added,
        crawl_failures=crawl_failures,
        pagespeed_scans_queued=len(pagespeed_jobs),
        pagespeed_jobs=[
            serialize_pagespeed_scan_job(job) for job in pagespeed_jobs
        ],
    )
