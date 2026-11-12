import asyncio
from collections.abc import Sequence
from datetime import datetime, timezone
from uuid import UUID
from urllib.parse import urlsplit, urlunsplit

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload
from starlette.concurrency import run_in_threadpool

from app.models import (
    Company,
    Contact,
    LeadSource,
    PageSpeedScanJob,
    SearchKeyword,
    WebsiteCrawl,
)
from app.schemas.discovery import (
    DiscoveryCrawlFailure,
    DiscoveryIngestRequest,
    DiscoveryIngestResponse,
    DiscoveryResult,
)
from app.services.discovery import search_companies
from app.services.normalization import normalize_company_name, normalize_domain
from app.services.pagespeed import PAGESPEED_STRATEGIES
from app.services.pagespeed_jobs import (
    enqueue_pagespeed_jobs,
    serialize_pagespeed_scan_job,
)
from app.services.website_crawler import CrawlData, crawl_website


def _persist_search_results(
    results: Sequence[DiscoveryResult],
    crawls_by_url: dict[str, CrawlData],
    pagespeed_targets_by_source: dict[str, str],
    db: Session,
    query: str,
    search_id: UUID | None,
) -> tuple[int, int, int, int, int, list[PageSpeedScanJob]]:
    for attempt in range(2):
        companies_created = 0
        sources_added = 0
        skipped = 0
        contacts_added = 0
        enriched_company_ids: set[UUID] = set()
        persisted_crawl_urls: set[str] = set()
        pagespeed_jobs_by_key: dict[tuple[str, str], PageSpeedScanJob] = {}
        pagespeed_jobs: list[PageSpeedScanJob] = []
        claimed_domains: set[str] = set()
        seen_urls: set[str] = set()
        linked_company_ids: set[UUID] = set()

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

                existing_source = db.scalar(
                    select(LeadSource)
                    .where(LeadSource.source_url == source_url)
                    .limit(1)
                )
                if existing_source is not None:
                    skipped += 1
                    company = existing_source.company
                else:
                    candidate_name = result.title.strip()[:200]
                    normalized_name = normalize_company_name(candidate_name)
                    if not candidate_name or not normalized_name:
                        skipped += 1
                        continue

                    company = db.scalar(
                        select(Company)
                        .where(Company.normalized_name == normalized_name)
                        .limit(1)
                    )
                    if company is None:
                        company = Company(
                            name=candidate_name,
                            normalized_name=normalized_name,
                            lead_status="needs_review",
                        )
                        db.add(company)
                        db.flush()
                        companies_created += 1

                if company.id not in linked_company_ids:
                    if all(
                        keyword.id != search_keyword.id
                        for keyword in company.search_keywords
                    ):
                        company.search_keywords.append(search_keyword)
                    linked_company_ids.add(company.id)

                crawl = crawls_by_url.get(source_url)
                if crawl is not None:
                    homepage = _homepage_url(crawl.website_url)
                    if not company.website:
                        company.website = homepage
                        enriched_company_ids.add(company.id)

                    domain = normalize_domain(company.website)
                    if (
                        not company.domain
                        and domain
                        and domain not in claimed_domains
                    ):
                        domain_owner = db.scalar(
                            select(Company.id)
                            .where(Company.domain == domain)
                            .where(Company.id != company.id)
                            .limit(1)
                        )
                        if domain_owner is None:
                            company.domain = domain
                            enriched_company_ids.add(company.id)
                            claimed_domains.add(domain)

                    existing_emails = {
                        contact.business_email.casefold()
                        for contact in company.contacts
                        if contact.business_email
                    }
                    for email in crawl.emails:
                        normalized_email = email.strip().lower()
                        if not normalized_email:
                            continue
                        if normalized_email in existing_emails:
                            continue
                        company.contacts.append(
                            Contact(
                                business_email=normalized_email,
                                is_publicly_listed=True,
                            )
                        )
                        existing_emails.add(normalized_email)
                        contacts_added += 1
                        enriched_company_ids.add(company.id)

                    if crawl.website_url not in persisted_crawl_urls:
                        _upsert_crawl(crawl, db)
                        persisted_crawl_urls.add(crawl.website_url)

                website_url = pagespeed_targets_by_source.get(
                    source_url,
                    _homepage_url(source_url),
                )
                for strategy in PAGESPEED_STRATEGIES:
                    job_key = (website_url, strategy)
                    job = pagespeed_jobs_by_key.get(job_key)
                    if job is None:
                        job = PageSpeedScanJob(
                            company_id=company.id,
                            website_url=website_url,
                            strategy=strategy,
                            companies=[company],
                        )
                        db.add(job)
                        pagespeed_jobs_by_key[job_key] = job
                        pagespeed_jobs.append(job)
                    elif all(
                        linked_company.id != company.id
                        for linked_company in job.companies
                    ):
                        job.companies.append(company)

                if existing_source is None:
                    evidence = result.content
                    if crawl is not None:
                        evidence = _combine_evidence(result.content, crawl)
                    source_domain = normalize_domain(source_url)
                    if source_domain is None:
                        raise ValueError(
                            f"Could not determine source domain for {source_url!r}."
                        )
                    db.add(
                        LeadSource(
                            company_id=company.id,
                            source_url=source_url,
                            source_domain=source_domain,
                            source_type="web_search",
                            evidence=evidence,
                        )
                    )
                    sources_added += 1

            db.commit()
            return (
                companies_created,
                sources_added,
                skipped,
                len(enriched_company_ids),
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


def _combine_evidence(search_evidence: str, crawl: CrawlData) -> str:
    sections = [search_evidence.strip()]
    for label, content in (
        ("About", crawl.about_content),
        ("Contact", crawl.contact_content),
        ("Mission and goals", crawl.goals_content),
    ):
        if content:
            sections.append(f"{label} page:\n{content}")
    return "\n\n".join(section for section in sections if section)


def _upsert_crawl(crawl: CrawlData, db: Session) -> None:
    record = db.scalar(
        select(WebsiteCrawl)
        .where(WebsiteCrawl.website_url == crawl.website_url)
        .limit(1)
    )
    if record is None:
        record = WebsiteCrawl(website_url=crawl.website_url)
        db.add(record)

    record.about_page_url = crawl.about_page_url
    record.about_content = crawl.about_content
    record.contact_page_url = crawl.contact_page_url
    record.contact_content = crawl.contact_content
    record.goals_page_url = crawl.goals_page_url
    record.goals_content = crawl.goals_content
    record.emails = crawl.emails
    record.social_links = crawl.social_links
    record.crawled_at = datetime.now(timezone.utc)


def _pagespeed_target_url(result: DiscoveryResult, db: Session) -> str:
    source = db.scalar(
        select(LeadSource)
        .options(selectinload(LeadSource.company))
        .where(LeadSource.source_url == str(result.url))
    )
    if source and source.company.website:
        return _homepage_url(source.company.website)

    normalized_name = normalize_company_name(result.title.strip()[:200])
    if normalized_name:
        company = db.scalar(
            select(Company)
            .where(Company.normalized_name == normalized_name)
            .limit(1)
        )
        if company and company.website:
            return _homepage_url(company.website)

    return _homepage_url(str(result.url))


async def ingest_search_results(
    request: DiscoveryIngestRequest,
    db: Session,
) -> DiscoveryIngestResponse:
    if request.search_id is not None:
        search_keyword = db.get(SearchKeyword, request.search_id)
        if search_keyword is None or search_keyword.keyword != request.query:
            raise HTTPException(
                status_code=422,
                detail="search_id does not match a recorded search keyword.",
            )

    if request.results is None:
        search_response = await search_companies(
            query=request.query,
            max_results=request.max_results,
        )
        results = search_response.results
    else:
        results = request.results

    semaphore = asyncio.Semaphore(10)
    crawl_failures: list[DiscoveryCrawlFailure] = []
    crawls_by_url: dict[str, CrawlData] = {}

    async def crawl_result(
        result: DiscoveryResult,
    ) -> tuple[str, CrawlData | None, DiscoveryCrawlFailure | None]:
        source_url = str(result.url)
        async with semaphore:
            try:
                return source_url, await crawl_website(source_url), None
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

    pagespeed_targets_by_source = {
        str(result.url): _pagespeed_target_url(result, db)
        for result in unique_results
    }
    crawl_results = await crawl_results_task
    for source_url, crawl, failure in crawl_results:
        if crawl is not None:
            crawls_by_url[source_url] = crawl
        if failure is not None:
            crawl_failures.append(failure)

    (
        companies_created,
        sources_added,
        skipped,
        companies_enriched,
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
        candidates_created=companies_created,
        sources_added=sources_added,
        results_skipped=skipped,
        websites_crawled=len(crawls_by_url),
        companies_enriched=companies_enriched,
        contacts_added=contacts_added,
        crawl_failures=crawl_failures,
        pagespeed_scans_queued=len(pagespeed_jobs),
        pagespeed_jobs=[
            serialize_pagespeed_scan_job(job) for job in pagespeed_jobs
        ],
    )
