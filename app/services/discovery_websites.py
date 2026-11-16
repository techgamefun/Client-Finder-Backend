from datetime import datetime, timezone
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    SearchKeyword,
    WebsiteCrawlJob,
    WebsiteHypeStatProfile,
)
from app.schemas.discovery import (
    DiscoveryWebsiteInput,
    DiscoveryWebsiteSaveRequest,
    DiscoveryWebsiteSaveResponse,
)
from app.services.crawl_jobs import create_website_crawl_job
from app.services.normalization import normalize_domain
from app.services.hypestat import fetch_hypestat_profiles
from app.services.application_settings import get_hypestat_settings
from app.services.website_registry import (
    canonical_website_url,
    upsert_website,
    website_favicon_url,
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
    else:
        search_keyword = SearchKeyword(
            keyword=request.query,
            searched_at=datetime.now(timezone.utc),
        )
        db.add(search_keyword)
        db.flush()

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
    hypestat_targets: list[tuple[UUID, str]] = []
    hypestat_settings = get_hypestat_settings(db)
    hypestat_profiles_reused = 0
    crawl_jobs_already_present = 0
    for result, canonical_url in unique_results:
        website = upsert_website(
            db,
            canonical_url,
            discovery_source=result.provider.value,
            discovery_provider=result.provider.value,
            favicon_url=website_favicon_url(canonical_url),
        )
        if all(
            keyword.id != search_keyword.id
            for keyword in website.search_keywords
        ):
            website.search_keywords.append(search_keyword)
        if hypestat_settings.auto_fetch_enabled:
            hypestat_profile = db.get(WebsiteHypeStatProfile, website.id)
            if hypestat_profile is None:
                hypestat_profile = WebsiteHypeStatProfile(
                    website_id=website.id,
                    status="pending",
                )
                db.add(hypestat_profile)
            if hypestat_profile.status != "succeeded":
                hypestat_profile.status = "pending"
                hypestat_profile.error = None
                hypestat_targets.append((website.id, website.domain))
            else:
                hypestat_profiles_reused += 1

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

    hypestat_results = (
        await fetch_hypestat_profiles(
            [domain for _, domain in hypestat_targets],
            hypestat_settings.api_key,
        )
        if hypestat_targets
        else {}
    )
    hypestat_checked_at = datetime.now(timezone.utc)
    for website_id, domain in hypestat_targets:
        hypestat_profile = db.get(WebsiteHypeStatProfile, website_id)
        if hypestat_profile is None:
            raise RuntimeError(
                f"HypeStat profile row disappeared for website {website_id}."
            )
        result = hypestat_results.get(domain)
        if result is None:
            result = hypestat_results.get(domain.casefold())
        if result is None:
            result_status = "failed"
            result_payload = None
            result_error = "HypeStat lookup returned no result for this website."
        else:
            result_status = result.status
            result_payload = result.payload
            result_error = result.error
        hypestat_profile.status = result_status
        hypestat_profile.payload = result_payload
        hypestat_profile.error = result_error
        hypestat_profile.checked_at = hypestat_checked_at
        hypestat_profile.fetched_at = (
            hypestat_checked_at if result_status == "succeeded" else None
        )
    if hypestat_targets:
        db.commit()

    hypestat_statuses = [
        (hypestat_results.get(domain) or hypestat_results.get(domain.casefold()))
        for _, domain in hypestat_targets
    ]
    return DiscoveryWebsiteSaveResponse(
        query=request.query,
        results_found=len(request.results),
        websites_saved=len(unique_results),
        crawl_jobs_enqueued=len(crawl_queue),
        crawl_jobs_already_present=crawl_jobs_already_present,
        domains_skipped=len(request.results) - len(unique_results),
        hypestat_profiles_requested=len(hypestat_targets),
        hypestat_profiles_reused=hypestat_profiles_reused,
        hypestat_profiles_succeeded=sum(
            result is not None and result.status == "succeeded"
            for result in hypestat_statuses
        ),
        hypestat_profiles_queued=sum(
            result is not None and result.status == "queued"
            for result in hypestat_statuses
        ),
        hypestat_profiles_failed=sum(
            result is None or result.status not in {"succeeded", "queued"}
            for result in hypestat_statuses
        ),
    )
