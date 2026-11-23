from typing import Annotated, Literal
from uuid import UUID

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Query,
    status,
)
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.api.deps import get_db, require_authenticated_context
from app.models import PageSpeedScan, Website, WebsiteCrawlJob
from app.schemas.job_queue import BatchJobRetryRequest, BatchJobRetryResponse
from app.schemas.job_queue import QueueJobsPageResponse
from app.schemas.pagespeed import PageSpeedScanSummary
from app.schemas.website import (
    WebsiteAddToLeadsRequest,
    WebsiteAddToLeadsResponse,
    WebsiteCorpusWordsResponse,
    WebsiteCorpusAnalysisJobResponse,
    WebsiteCrawlJobHistoryResponse,
    WebsiteDetailResponse,
    WebsiteDiscoveryKeywordResponse,
    WebsiteHypeStatProfileResponse,
    WebsiteAhrefsMetricsResponse,
    WebsiteBatchStatusUpdateRequest,
    WebsiteBatchStatusUpdateResponse,
    WebsiteReclassificationBatchResponse,
    WebsiteRegistryFacetsResponse,
    WebsiteRegistryPageResponse,
    WebsiteResponse,
    WebsiteUpdate,
)
from app.schemas.website_analytics import WebsiteAnalyticsResponse
from app.schemas.website_crawl import (
    CrawlPageClassification,
    WebsiteCrawlResponse,
)
from app.services.website_corpus_words import (
    get_website_corpus_words,
    queue_stale_website_corpus_analysis,
)
from app.services.website_corpus_jobs import (
    enqueue_website_corpus_analysis_jobs,
    list_website_corpus_analysis_jobs,
    retry_failed_website_corpus_analysis_jobs,
    retry_website_corpus_analysis_job,
)
from app.services.website_classification import (
    reclassify_saved_website,
    reclassify_unclassified_websites,
)
from app.services.website_leads import add_websites_to_leads
from app.services.website_registry import update_websites_status
from app.services.ahrefs import (
    fetch_website_ahrefs_metrics,
    get_cached_website_ahrefs_metrics,
)
from app.services.website_hypestat import refetch_website_hypestat_profile
from app.services.website_registry_query import (
    WebsiteSortKey,
    get_website_registry_facets,
    get_website_registry_page,
)
from app.services.website_analytics import build_website_analytics
from app.tenant_context import TenantContext


router = APIRouter(prefix="/websites", tags=["Websites"])

WebsiteSession = Annotated[Session, Depends(get_db)]
CurrentContext = Annotated[
    TenantContext,
    Depends(require_authenticated_context),
]
PAGESPEED_HISTORY_LIMIT = 10


@router.get("/analytics", response_model=WebsiteAnalyticsResponse)
def read_website_analytics(
    context: CurrentContext,
    db: WebsiteSession,
) -> WebsiteAnalyticsResponse:
    return build_website_analytics(db, context)


@router.get("", response_model=list[WebsiteResponse])
def list_websites(
    db: WebsiteSession,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    page_type: str | None = Query(default=None, max_length=64),
    search: Annotated[str, Query(max_length=300)] = "",
) -> list[WebsiteResponse]:
    statement = (
        select(Website)
        .options(
            selectinload(Website.crawl),
            selectinload(Website.hypestat_profile),
        )
        .order_by(Website.created_at.desc(), Website.id.desc())
        .limit(limit)
        .offset(offset)
    )
    if page_type is not None:
        statement = statement.where(Website.page_type == page_type)
    if search:
        statement = statement.where(
            Website.website_url.contains(search, autoescape=True)
            | Website.domain.contains(search, autoescape=True)
        )

    websites = list(db.scalars(statement).all())
    crawl_job_statuses = _crawl_job_statuses(db, websites)
    return [
        _website_response(
            website,
            crawl_job_statuses.get(website.website_url),
        )
        for website in websites
    ]


@router.get("/registry-page", response_model=WebsiteRegistryPageResponse)
def list_website_registry_page(
    db: WebsiteSession,
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    search: Annotated[str, Query(max_length=300)] = "",
    status_filter: Literal["new", "lead", "checked"] | None = Query(
        default=None,
        alias="status",
    ),
    page_type: str | None = Query(default=None, max_length=64),
    category: str | None = Query(default=None, max_length=100),
    discovery_source: str | None = Query(default=None, max_length=255),
    minimum_monthly_visits: float | None = Query(default=None, ge=0),
    minimum_referring_domains: float | None = Query(default=None, ge=0),
    maximum_global_rank: float | None = Query(default=None, ge=1),
    pagespeed_strategy: Literal["desktop", "mobile"] | None = Query(
        default=None,
    ),
    pagespeed_category: Literal[
        "performance",
        "accessibility",
        "best-practices",
        "seo",
    ]
    | None = Query(default=None),
    minimum_pagespeed_score: int | None = Query(
        default=None,
        ge=0,
        le=100,
    ),
    maximum_pagespeed_score: int | None = Query(
        default=None,
        ge=0,
        le=100,
    ),
    minimum_ahrefs_domain_rating: float | None = Query(
        default=None,
        ge=0,
        le=100,
    ),
    maximum_ahrefs_domain_rating: float | None = Query(
        default=None,
        ge=0,
        le=100,
    ),
    hypestat_country: str | None = Query(default=None, max_length=100),
    backlink_country: str | None = Query(default=None, max_length=100),
    audience_country: str | None = Query(default=None, max_length=100),
    sort_by: WebsiteSortKey | None = Query(default=None),
    sort_direction: Literal["ascending", "descending"] = "ascending",
) -> WebsiteRegistryPageResponse:
    if (
        minimum_pagespeed_score is not None
        or maximum_pagespeed_score is not None
    ) and (pagespeed_strategy is None or pagespeed_category is None):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "Choose a PageSpeed device and category when filtering scores."
            ),
        )
    if (
        minimum_pagespeed_score is not None
        and maximum_pagespeed_score is not None
        and minimum_pagespeed_score > maximum_pagespeed_score
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Minimum PageSpeed score cannot exceed the maximum.",
        )
    websites, total_count, indexed_count, website_count = (
        get_website_registry_page(
            db,
            limit=limit,
            offset=offset,
            search=search,
            status=status_filter,
            page_type=page_type,
            category=category,
            discovery_source=discovery_source,
            minimum_monthly_visits=minimum_monthly_visits,
            minimum_referring_domains=minimum_referring_domains,
            maximum_global_rank=maximum_global_rank,
            pagespeed_strategy=pagespeed_strategy,
            pagespeed_category=pagespeed_category,
            minimum_pagespeed_score=minimum_pagespeed_score,
            maximum_pagespeed_score=maximum_pagespeed_score,
            minimum_ahrefs_domain_rating=minimum_ahrefs_domain_rating,
            maximum_ahrefs_domain_rating=maximum_ahrefs_domain_rating,
            hypestat_country=hypestat_country,
            backlink_country=backlink_country,
            audience_country=audience_country,
            sort_by=sort_by,
            sort_direction=sort_direction,
        )
    )
    crawl_job_statuses = _crawl_job_statuses(db, websites)
    pagespeed_scans = _latest_pagespeed_scans(db, websites)
    return WebsiteRegistryPageResponse(
        items=[
            _website_response(
                website,
                crawl_job_statuses.get(website.website_url),
                pagespeed_desktop=pagespeed_scans.get(website.id, {}).get(
                    "desktop",
                ),
                pagespeed_mobile=pagespeed_scans.get(website.id, {}).get(
                    "mobile",
                ),
                include_crawl_signals=False,
                include_created_by=False,
            )
            for website in websites
        ],
        total_count=total_count,
        indexed_count=indexed_count,
        website_count=website_count,
    )


@router.get(
    "/registry-facets",
    response_model=WebsiteRegistryFacetsResponse,
)
def list_website_registry_facets(
    db: WebsiteSession,
) -> WebsiteRegistryFacetsResponse:
    return WebsiteRegistryFacetsResponse(**get_website_registry_facets(db))


@router.get("/corpus-words", response_model=WebsiteCorpusWordsResponse)
def website_corpus_words(
    background_tasks: BackgroundTasks,
    db: WebsiteSession,
    website_ids: list[UUID] | None = Query(default=None, max_length=100),
) -> WebsiteCorpusWordsResponse:
    job_ids = queue_stale_website_corpus_analysis(db, website_ids=website_ids)
    if job_ids:
        background_tasks.add_task(enqueue_website_corpus_analysis_jobs, job_ids)
    return get_website_corpus_words(db, website_ids=website_ids)


@router.get(
    "/corpus-words/jobs",
    response_model=QueueJobsPageResponse[WebsiteCorpusAnalysisJobResponse],
)
def website_corpus_analysis_jobs(
    db: WebsiteSession,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
    search: Annotated[str, Query(max_length=300)] = "",
    status_filter: Literal[
        "queued",
        "running",
        "succeeded",
        "failed",
        "skipped",
    ]
    | None = Query(default=None, alias="status"),
) -> QueueJobsPageResponse[WebsiteCorpusAnalysisJobResponse]:
    return list_website_corpus_analysis_jobs(
        db,
        page=page,
        page_size=page_size,
        search=search,
        status=status_filter,
    )


@router.post(
    "/corpus-words/jobs/retry-batch",
    response_model=BatchJobRetryResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def retry_website_corpus_analysis_batch(
    payload: BatchJobRetryRequest,
    db: WebsiteSession,
) -> BatchJobRetryResponse:
    return await retry_failed_website_corpus_analysis_jobs(
        payload.job_ids,
        db,
    )


@router.post(
    "/corpus-words/jobs/{job_id}/retry",
    response_model=WebsiteCorpusAnalysisJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def retry_website_corpus_analysis(
    job_id: UUID,
    db: WebsiteSession,
) -> WebsiteCorpusAnalysisJobResponse:
    return await retry_website_corpus_analysis_job(job_id, db)


@router.post(
    "/add-to-leads",
    response_model=WebsiteAddToLeadsResponse,
)
def add_to_leads(
    payload: WebsiteAddToLeadsRequest,
    db: WebsiteSession,
) -> WebsiteAddToLeadsResponse:
    try:
        result = add_websites_to_leads(
            db,
            payload.website_ids,
            batch_id=payload.batch_id,
            batch_name=payload.batch_name,
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "A company for one of these domains was added concurrently. "
                "Refresh the websites and retry."
            ),
        ) from exc
    return result


@router.patch(
    "/batch-status",
    response_model=WebsiteBatchStatusUpdateResponse,
)
def batch_update_status(
    payload: WebsiteBatchStatusUpdateRequest,
    db: WebsiteSession,
) -> WebsiteBatchStatusUpdateResponse:
    updated_count, not_found_count = update_websites_status(
        db,
        payload.website_ids,
        payload.status,
    )
    db.commit()
    return WebsiteBatchStatusUpdateResponse(
        requested_count=len(set(payload.website_ids)),
        updated_count=updated_count,
        not_found_count=not_found_count,
    )


@router.post(
    "/reclassify-unclassified",
    response_model=WebsiteReclassificationBatchResponse,
)
async def reclassify_unclassified(
    db: WebsiteSession,
) -> WebsiteReclassificationBatchResponse:
    return await reclassify_unclassified_websites(db)


@router.post("/{website_id}/reclassify", response_model=WebsiteResponse)
async def reclassify_website(
    website_id: UUID,
    db: WebsiteSession,
) -> WebsiteResponse:
    website = await reclassify_saved_website(website_id, db)
    crawl_job_status = _crawl_job_statuses(db, [website]).get(website.website_url)
    return _website_response(website, crawl_job_status)


@router.get("/{website_id}", response_model=WebsiteDetailResponse)
def get_website_detail(
    website_id: UUID,
    db: WebsiteSession,
) -> WebsiteDetailResponse:
    website = db.scalar(
        select(Website)
        .options(
            selectinload(Website.crawl),
            selectinload(Website.hypestat_profile),
            selectinload(Website.search_keywords),
        )
        .where(Website.id == website_id)
    )
    if website is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Website not found.",
        )

    crawl_jobs = db.scalars(
        select(WebsiteCrawlJob)
        .where(WebsiteCrawlJob.website_url == website.website_url)
        .order_by(WebsiteCrawlJob.created_at.desc(), WebsiteCrawlJob.id.desc())
    ).all()
    pagespeed_scans = db.scalars(
        select(PageSpeedScan)
        .where(PageSpeedScan.website_id == website.id)
        .order_by(PageSpeedScan.scanned_at.desc(), PageSpeedScan.id.desc())
        .limit(PAGESPEED_HISTORY_LIMIT)
    ).all()
    crawl_job_status = crawl_jobs[0].status if crawl_jobs else None
    ahrefs_metrics = get_cached_website_ahrefs_metrics(website.id, db)
    return WebsiteDetailResponse(
        website=_website_response(website, crawl_job_status),
        crawl=(
            WebsiteCrawlResponse.model_validate(website.crawl)
            if website.crawl is not None
            else None
        ),
        pagespeed_scans=[
            PageSpeedScanSummary.model_validate(scan)
            for scan in pagespeed_scans
        ],
        hypestat_profile=(
            WebsiteHypeStatProfileResponse.model_validate(
                website.hypestat_profile
            )
            if website.hypestat_profile is not None
            else None
        ),
        ahrefs_metrics=(
            WebsiteAhrefsMetricsResponse.model_validate(ahrefs_metrics)
            if ahrefs_metrics is not None
            else None
        ),
        crawl_jobs=[
            WebsiteCrawlJobHistoryResponse.model_validate(job)
            for job in crawl_jobs
        ],
        discovery_keywords=[
            WebsiteDiscoveryKeywordResponse.model_validate(keyword)
            for keyword in website.search_keywords
        ],
    )


@router.get(
    "/{website_id}/pagespeed-scans/{scan_id}",
    response_model=PageSpeedScanSummary,
)
def get_website_pagespeed_scan(
    website_id: UUID,
    scan_id: UUID,
    db: WebsiteSession,
) -> PageSpeedScan:
    if db.get(Website, website_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Website not found.",
        )

    scan = db.scalar(
        select(PageSpeedScan).where(
            PageSpeedScan.id == scan_id,
            PageSpeedScan.website_id == website_id,
        )
    )
    if scan is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="PageSpeed scan not found.",
        )
    return scan


@router.post(
    "/{website_id}/hypestat/refetch",
    response_model=WebsiteHypeStatProfileResponse,
)
async def refetch_hypestat_profile(
    website_id: UUID,
    db: WebsiteSession,
) -> WebsiteHypeStatProfileResponse:
    profile = await refetch_website_hypestat_profile(website_id, db)
    return WebsiteHypeStatProfileResponse.model_validate(profile)


@router.post(
    "/{website_id}/ahrefs-metrics",
    response_model=WebsiteAhrefsMetricsResponse,
    response_model_exclude_none=True,
)
async def refresh_ahrefs_metrics(
    website_id: UUID,
    db: WebsiteSession,
) -> WebsiteAhrefsMetricsResponse:
    return await fetch_website_ahrefs_metrics(website_id, db)


@router.patch("/{website_id}", response_model=WebsiteResponse)
def update_website(
    website_id: UUID,
    payload: WebsiteUpdate,
    db: WebsiteSession,
) -> WebsiteResponse:
    website = db.scalar(
        select(Website)
        .options(selectinload(Website.crawl))
        .where(Website.id == website_id)
    )
    if website is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Website not found.",
        )

    changes = payload.model_dump(exclude_unset=True)
    if "page_type" in changes:
        website.classification_manual = True
        if changes["page_type"] == "business_listing":
            website.discovery_source = "listing_website"
        else:
            if website.discovery_source == "listing_website":
                website.discovery_source = website.discovery_provider or "manual"
    for field, value in changes.items():
        setattr(website, field, value)
    db.commit()
    db.refresh(website)
    crawl_job_status = _crawl_job_statuses(db, [website]).get(website.website_url)
    return _website_response(website, crawl_job_status)


def _crawl_job_statuses(
    db: Session,
    websites: list[Website],
) -> dict[str, str]:
    website_urls = {website.website_url for website in websites}
    if not website_urls:
        return {}

    jobs = db.execute(
        select(
            WebsiteCrawlJob.website_url,
            WebsiteCrawlJob.status,
        )
        .where(WebsiteCrawlJob.website_url.in_(website_urls))
        .order_by(WebsiteCrawlJob.created_at.desc(), WebsiteCrawlJob.id.desc())
    ).all()
    statuses: dict[str, str] = {}
    for website_url, status in jobs:
        statuses.setdefault(website_url, status)
    return statuses


def _latest_pagespeed_scans(
    db: Session,
    websites: list[Website],
) -> dict[UUID, dict[str, PageSpeedScan]]:
    website_ids = [website.id for website in websites]
    if not website_ids:
        return {}
    ranked_scans = (
        select(
            PageSpeedScan.id.label("scan_id"),
            func.row_number()
            .over(
                partition_by=(
                    PageSpeedScan.website_id,
                    PageSpeedScan.strategy,
                ),
                order_by=(
                    PageSpeedScan.scanned_at.desc(),
                    PageSpeedScan.id.desc(),
                ),
            )
            .label("scan_rank"),
        )
        .where(
            PageSpeedScan.website_id.in_(website_ids),
            PageSpeedScan.organization_id == websites[0].organization_id,
        )
        .subquery()
    )
    scans = db.scalars(
        select(PageSpeedScan)
        .join(ranked_scans, ranked_scans.c.scan_id == PageSpeedScan.id)
        .where(ranked_scans.c.scan_rank == 1)
    ).all()
    latest: dict[UUID, dict[str, PageSpeedScan]] = {}
    for scan in scans:
        if scan.website_id is not None:
            latest.setdefault(scan.website_id, {})[scan.strategy] = scan
    return latest


def _website_response(
    website: Website,
    crawl_job_status: str | None,
    *,
    pagespeed_desktop: PageSpeedScan | None = None,
    pagespeed_mobile: PageSpeedScan | None = None,
    include_crawl_signals: bool = True,
    include_created_by: bool = True,
) -> WebsiteResponse:
    crawl_signals = []
    if include_crawl_signals and website.crawl is not None:
        crawl_signals = [
            CrawlPageClassification.model_validate(page)
            for page in website.crawl.classification_signals
        ]
    hypestat_summary = _website_hypestat_summary(
        website.hypestat_profile.payload
        if website.hypestat_profile is not None
        else None
    )
    return WebsiteResponse(
        id=website.id,
        website_url=website.website_url,
        domain=website.domain,
        favicon_url=website.favicon_url,
        crawl_id=website.crawl_id,
        is_crawled=website.crawl_id is not None,
        crawl_job_status=(
            crawl_job_status
            or ("succeeded" if website.crawl_id is not None else None)
        ),
        category=website.category,
        page_type=website.page_type,
        site_topic=website.site_topic,
        language=website.language,
        status=website.status,
        discovery_source=website.discovery_source,
        discovery_provider=website.discovery_provider,
        created_at=website.created_at,
        created_by_email=(
            website.created_by_user.email
            if include_created_by and website.created_by_user is not None
            else None
        ),
        crawled_at=website.crawl.crawled_at if website.crawl else None,
        crawl_signals=crawl_signals,
        ahrefs_domain_rating=website.ahrefs_domain_rating,
        ahrefs_domain_rating_license_url=(
            website.ahrefs_domain_rating_license_url
        ),
        ahrefs_domain_rating_fetched_at=(
            website.ahrefs_domain_rating_fetched_at
        ),
        pagespeed_desktop=(
            PageSpeedScanSummary.model_validate(pagespeed_desktop)
            if pagespeed_desktop is not None
            else None
        ),
        pagespeed_mobile=(
            PageSpeedScanSummary.model_validate(pagespeed_mobile)
            if pagespeed_mobile is not None
            else None
        ),
        **hypestat_summary,
    )


def _website_hypestat_summary(
    payload: dict[str, object] | None,
) -> dict[str, object]:
    empty_summary: dict[str, object] = {
        "hypestat_monthly_visits": None,
        "hypestat_referring_domains": None,
        "hypestat_global_rank": None,
        "hypestat_country": None,
        "hypestat_referring_domains_by_country": [],
        "hypestat_audience_countries": [],
    }
    if payload is None:
        return empty_summary

    entry = _as_record(payload.get("entry")) or payload
    data = _as_record(entry.get("data"))
    if data is None:
        return empty_summary

    traffic = _as_record(data.get("traffic")) or {}
    rank = _as_record(data.get("rank")) or {}
    country_rank = _as_record(rank.get("country")) or {}
    backlinks = _as_record(data.get("backlinks")) or {}
    referring_domains_by_country = []
    for country_row in _as_records(backlinks.get("by_country")):
        country = country_row.get("country")
        domains = country_row.get("domains")
        if (
            isinstance(country, str)
            and country.strip()
            and isinstance(domains, int)
            and not isinstance(domains, bool)
        ):
            referring_domains_by_country.append(
                {"country": country, "domains": domains}
            )

    audience_countries = []
    raw_audience_countries = traffic.get("countries")
    for country_row in _as_records(raw_audience_countries):
        country = country_row.get("country") or country_row.get("name")
        if isinstance(country, str) and country.strip():
            audience_countries.append(country)
    if isinstance(raw_audience_countries, list):
        audience_countries.extend(
            country
            for country in raw_audience_countries
            if isinstance(country, str) and country.strip()
        )

    country = country_rank.get("country")
    return {
        "hypestat_monthly_visits": _int_or_none(traffic.get("visits_monthly")),
        "hypestat_referring_domains": _int_or_none(
            backlinks.get("referring_domains")
        ),
        "hypestat_global_rank": _int_or_none(rank.get("hypestat")),
        "hypestat_country": country if isinstance(country, str) else None,
        "hypestat_referring_domains_by_country": referring_domains_by_country,
        "hypestat_audience_countries": list(dict.fromkeys(audience_countries)),
    }


def _as_record(value: object) -> dict[str, object] | None:
    return value if isinstance(value, dict) else None


def _as_records(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [record for item in value if (record := _as_record(item)) is not None]


def _int_or_none(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None
