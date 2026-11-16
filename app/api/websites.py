from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.api.deps import get_db
from app.models import Website, WebsiteCrawlJob
from app.schemas.website import (
    WebsiteAddToLeadsRequest,
    WebsiteAddToLeadsResponse,
    WebsiteCorpusWordsResponse,
    WebsiteCrawlJobHistoryResponse,
    WebsiteDetailResponse,
    WebsiteDiscoveryKeywordResponse,
    WebsiteHypeStatProfileResponse,
    WebsiteReclassificationBatchResponse,
    WebsiteResponse,
    WebsiteUpdate,
)
from app.schemas.website_crawl import (
    CrawlPageClassification,
    WebsiteCrawlResponse,
)
from app.services.website_corpus_words import get_website_corpus_words
from app.services.website_classification import (
    reclassify_saved_website,
    reclassify_unclassified_websites,
)
from app.services.website_leads import add_websites_to_leads
from app.services.website_hypestat import refetch_website_hypestat_profile


router = APIRouter(prefix="/websites", tags=["Websites"])

WebsiteSession = Annotated[Session, Depends(get_db)]


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


@router.get("/corpus-words", response_model=WebsiteCorpusWordsResponse)
def website_corpus_words(
    db: WebsiteSession,
) -> WebsiteCorpusWordsResponse:
    return get_website_corpus_words(db)


@router.post(
    "/add-to-leads",
    response_model=WebsiteAddToLeadsResponse,
)
def add_to_leads(
    payload: WebsiteAddToLeadsRequest,
    db: WebsiteSession,
) -> WebsiteAddToLeadsResponse:
    try:
        result = add_websites_to_leads(db, payload.website_ids)
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
    crawl_job_status = crawl_jobs[0].status if crawl_jobs else None
    return WebsiteDetailResponse(
        website=_website_response(website, crawl_job_status),
        crawl=(
            WebsiteCrawlResponse.model_validate(website.crawl)
            if website.crawl is not None
            else None
        ),
        hypestat_profile=(
            WebsiteHypeStatProfileResponse.model_validate(
                website.hypestat_profile
            )
            if website.hypestat_profile is not None
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

    jobs = db.scalars(
        select(WebsiteCrawlJob)
        .where(WebsiteCrawlJob.website_url.in_(website_urls))
        .order_by(WebsiteCrawlJob.created_at.desc(), WebsiteCrawlJob.id.desc())
    ).all()
    statuses: dict[str, str] = {}
    for job in jobs:
        statuses.setdefault(job.website_url, job.status)
    return statuses


def _website_response(
    website: Website,
    crawl_job_status: str | None,
) -> WebsiteResponse:
    crawl_signals = [
        CrawlPageClassification.model_validate(page)
        for page in (
            website.crawl.classification_signals
            if website.crawl is not None
            else []
        )
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
            if website.created_by_user is not None
            else None
        ),
        crawled_at=website.crawl.crawled_at if website.crawl else None,
        crawl_signals=crawl_signals,
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
