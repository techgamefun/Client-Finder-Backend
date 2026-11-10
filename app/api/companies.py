
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, selectinload

from app.api.deps import get_db
from app.models import Company, PageSpeedScan
from app.schemas.company import (
    CompanyCreate,
    CompanyResponse,
    CompanyUpdate,
    LeadStatusUpdate,
    ResearchSourceCrawlRequest,
)
from app.schemas.pagespeed import (
    PageSpeedScanRequest,
    PageSpeedScanResponse,
)
from app.schemas.website_crawl import WebsiteCrawlResponse
from app.services.normalization import (
    normalize_company_name,
    normalize_domain,
)
from app.services.research_source_crawler import crawl_research_source
from app.services.pagespeed import PageSpeedError, scan_pagespeed
from app.services.website_crawler import crawl_and_persist, get_website_crawl


router = APIRouter(
    prefix="/companies",
    tags=["Companies"],
)

DatabaseSession = Annotated[Session, Depends(get_db)]
COMPANY_DETAILS = (
    selectinload(Company.contacts),
    selectinload(Company.sources),
    selectinload(Company.search_keywords),
    selectinload(Company.pagespeed_scans),
)


def get_company_or_404(company_id: UUID, db: Session) -> Company:
    statement = (
        select(Company)
        .options(*COMPANY_DETAILS)
        .where(Company.id == company_id)
    )
    company = db.scalar(statement)
    if company is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Company not found.",
        )
    return company


@router.post(
    "",
    response_model=CompanyResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_company(
    payload: CompanyCreate,
    db: DatabaseSession,
) -> Company:
    company_data = payload.model_dump(mode="json")

    company_data["normalized_name"] = normalize_company_name(
        company_data["name"]
    )

    company_data["domain"] = normalize_domain(
        company_data.get("domain")
    )

    if not company_data["domain"] and company_data.get("website"):
        company_data["domain"] = normalize_domain(
            str(company_data["website"])
        )

    company = Company(**company_data)
    db.add(company)

    try:
        db.commit()
        db.refresh(company)
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A company with this domain already exists.",
        ) from exc

    return company


@router.get("", response_model=list[CompanyResponse])
def list_companies(
    db: DatabaseSession,
    limit: int = 50,
    offset: int = 0,
) -> list[Company]:
    if not 1 <= limit <= 100:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="limit must be between 1 and 100.",
        )

    if offset < 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="offset must be non-negative.",
        )

    statement = (
        select(Company)
        .options(*COMPANY_DETAILS)
        .order_by(Company.created_at.desc(), Company.id.desc())
        .limit(limit)
        .offset(offset)
    )

    return list(db.scalars(statement).all())


@router.get("/{company_id}", response_model=CompanyResponse)
def get_company(
    company_id: UUID,
    db: DatabaseSession,
) -> Company:
    return get_company_or_404(company_id, db)


def _company_website_url(company: Company) -> str | None:
    if company.website:
        return company.website
    if company.domain:
        return f"https://{company.domain}"
    return None


@router.get(
    "/{company_id}/website-crawl",
    response_model=WebsiteCrawlResponse | None,
)
def get_company_website_crawl(
    company_id: UUID,
    db: DatabaseSession,
) -> WebsiteCrawlResponse | None:
    company = db.get(Company, company_id)
    if company is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Company not found.",
        )

    website_url = _company_website_url(company)
    if website_url is None:
        return None

    crawl = get_website_crawl(website_url, db)
    return WebsiteCrawlResponse.model_validate(crawl) if crawl else None


@router.post(
    "/{company_id}/website-crawl",
    response_model=WebsiteCrawlResponse,
)
async def crawl_company_website(
    company_id: UUID,
    db: DatabaseSession,
) -> WebsiteCrawlResponse:
    company = db.get(Company, company_id)
    if company is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Company not found.",
        )

    website_url = _company_website_url(company)
    if website_url is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Add a company website or domain before crawling.",
        )

    crawl = await crawl_and_persist(website_url, db)
    return WebsiteCrawlResponse.model_validate(crawl)


@router.post(
    "/{company_id}/sources/crawl",
    response_model=CompanyResponse,
)
async def crawl_research_source_for_company(
    company_id: UUID,
    payload: ResearchSourceCrawlRequest,
    db: DatabaseSession,
) -> Company:
    if db.get(Company, company_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Company not found.",
        )

    await crawl_research_source(company_id, str(payload.source_url), db)
    return get_company_or_404(company_id, db)


@router.get(
    "/{company_id}/pagespeed-scans/{scan_id}",
    response_model=PageSpeedScanResponse,
)
def get_pagespeed_scan(
    company_id: UUID,
    scan_id: UUID,
    db: DatabaseSession,
) -> PageSpeedScan:
    if db.get(Company, company_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Company not found.",
        )

    scan = db.scalar(
        select(PageSpeedScan).where(
            PageSpeedScan.id == scan_id,
            PageSpeedScan.company_id == company_id,
        )
    )
    if scan is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="PageSpeed scan not found.",
        )
    return scan


@router.post(
    "/{company_id}/pagespeed-scans",
    response_model=PageSpeedScanResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_pagespeed_scan(
    company_id: UUID,
    payload: PageSpeedScanRequest,
    db: DatabaseSession,
) -> PageSpeedScan:
    company = db.get(Company, company_id)
    if company is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Company not found.",
        )

    website_url = _company_website_url(company)
    if website_url is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Add a company website or domain before scanning.",
        )

    try:
        result = await scan_pagespeed(website_url, payload.strategy)
    except PageSpeedError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc

    scan = PageSpeedScan(
        company_id=company.id,
        website_url=result.website_url,
        strategy=result.strategy,
        category_scores=result.category_scores,
        report=result.report,
    )
    db.add(scan)
    try:
        db.commit()
        db.refresh(scan)
    except SQLAlchemyError:
        db.rollback()
        raise

    return scan


@router.patch("/{company_id}", response_model=CompanyResponse)
def update_company(
    company_id: UUID,
    payload: CompanyUpdate,
    db: DatabaseSession,
) -> Company:
    company = get_company_or_404(company_id, db)
    company_data = payload.model_dump(exclude_unset=True, mode="json")

    if not company_data:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="At least one company field must be provided.",
        )

    if "name" in company_data:
        name = company_data["name"]
        if name is None or not name.strip():
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Company name cannot be blank.",
            )
        company_data["name"] = name.strip()
        company_data["normalized_name"] = normalize_company_name(name.strip())

    if "domain" in company_data or "website" in company_data:
        domain = normalize_domain(
            company_data.get("domain", company.domain)
        )
        website = company_data.get("website", company.website)
        if not domain and website:
            domain = normalize_domain(str(website))
        company_data["domain"] = domain

    for field, value in company_data.items():
        setattr(company, field, value)

    try:
        db.commit()
        db.refresh(company)
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A company with this domain already exists.",
        ) from exc
    except SQLAlchemyError:
        db.rollback()
        raise

    return get_company_or_404(company_id, db)


@router.patch("/{company_id}/status", response_model=CompanyResponse)
def update_lead_status(
    company_id: UUID,
    payload: LeadStatusUpdate,
    db: DatabaseSession,
) -> Company:
    company = db.get(Company, company_id)
    if company is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Company not found.",
        )

    company.lead_status = payload.lead_status
    try:
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        raise
    return get_company_or_404(company_id, db)


@router.delete(
    "/{company_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_company(
    company_id: UUID,
    db: DatabaseSession,
) -> None:
    company = db.get(Company, company_id)
    if company is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Company not found.",
        )

    db.delete(company)
    try:
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        raise
