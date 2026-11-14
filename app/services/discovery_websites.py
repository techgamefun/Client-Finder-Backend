from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import SearchKeyword, WebsiteCrawlJob
from app.schemas.discovery import (
    DiscoveryWebsiteInput,
    DiscoveryWebsiteSaveRequest,
    DiscoveryWebsiteSaveResponse,
)
from app.services.crawl_jobs import create_website_crawl_job
from app.services.normalization import normalize_domain
from app.services.website_registry import (
    canonical_website_url,
    upsert_website,
)


async def save_discovered_websites(
    request: DiscoveryWebsiteSaveRequest,
    db: Session,
) -> DiscoveryWebsiteSaveResponse:
    if request.search_id is not None:
        search_keyword = db.get(SearchKeyword, request.search_id)
        if search_keyword is None or search_keyword.keyword != request.query:
            raise HTTPException(
                status_code=422,
                detail="search_id does not match a recorded search keyword.",
            )

    unique_results: list[tuple[DiscoveryWebsiteInput, str]] = []
    seen_domains: set[str] = set()
    for result in request.results:
        source_url = str(result.url)
        domain = normalize_domain(source_url)
        if domain is None:
            raise HTTPException(
                status_code=422,
                detail=f"Could not determine a domain from {source_url!r}.",
            )
        if domain in seen_domains:
            continue
        seen_domains.add(domain)
        unique_results.append((result, canonical_website_url(source_url)))

    crawl_queue: list[tuple[DiscoveryWebsiteInput, str]] = []
    crawl_jobs_already_present = 0
    for result, canonical_url in unique_results:
        website = upsert_website(
            db,
            canonical_url,
            discovery_source=result.provider.value,
            discovery_provider=result.provider.value,
        )
        active_job = db.scalar(
            select(WebsiteCrawlJob.id)
            .where(
                WebsiteCrawlJob.website_url == website.website_url,
                WebsiteCrawlJob.status.in_(("queued", "running")),
            )
            .limit(1)
        )
        if website.crawl_id is not None or active_job is not None:
            crawl_jobs_already_present += 1
        else:
            crawl_queue.append((result, canonical_url))

    db.commit()

    for result, canonical_url in crawl_queue:
        await create_website_crawl_job(
            canonical_url,
            db,
            discovery_source=result.provider.value,
            discovery_provider=result.provider.value,
        )

    return DiscoveryWebsiteSaveResponse(
        query=request.query,
        results_found=len(request.results),
        websites_saved=len(unique_results),
        crawl_jobs_enqueued=len(crawl_queue),
        crawl_jobs_already_present=crawl_jobs_already_present,
        domains_skipped=len(request.results) - len(unique_results),
    )
