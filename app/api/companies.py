
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import exists, func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, selectinload
from starlette.concurrency import run_in_threadpool

from app.api.deps import get_db, require_authenticated_context
from app.models import Company, Contact, PageSpeedScan
from app.schemas.company import (
    CompanyCreate,
    CompanyNavigationSummaryResponse,
    CompanyPipelineResponse,
    CompanyPipelineSelectionResponse,
    CompanyResponse,
    CompanyUpdate,
    ContactCreate,
    ContactResponse,
    ContactUpdate,
    LeadStatusUpdate,
)
from app.schemas.pagespeed import (
    PageSpeedScanJobResponse,
    PageSpeedScanRequest,
    PageSpeedScanSummary,
)
from app.schemas.website_crawl import (
    WebsiteCrawlJobResponse,
    WebsiteCrawlResponse,
)
from app.services.crawl_jobs import (
    create_website_crawl_job,
    get_website_crawl_job,
)
from app.services.company_pipeline import (
    get_company_pipeline,
    get_company_pipeline_selection,
)
from app.services.normalization import (
    normalize_company_name,
    normalize_domain,
)
from app.services.pagespeed_jobs import get_pagespeed_scan_job
from app.services.pagespeed import PageSpeedError, PageSpeedScanData, scan_pagespeed
from app.services.application_settings import get_user_provider_api_key
from app.services.website_crawler import get_website_crawl
from app.services.website_registry import upsert_website
from app.tenant_context import TenantContext


router = APIRouter(
    prefix="/companies",
    tags=["Companies"],
)

DatabaseSession = Annotated[Session, Depends(get_db)]
CurrentContext = Annotated[
    TenantContext,
    Depends(require_authenticated_context),
]
COMPANY_DETAILS = (
    selectinload(Company.created_by_user),
    selectinload(Company.contacts),
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


def _get_company_for_contact_management(
    company_id: UUID,
    organization_id: UUID,
    db: Session,
) -> Company:
    company = db.scalar(
        select(Company).where(
            Company.id == company_id,
            Company.organization_id == organization_id,
        )
    )
    if company is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Company not found.",
        )
    return company


def _get_contact_or_404(
    company_id: UUID,
    contact_id: UUID,
    organization_id: UUID,
    db: Session,
) -> Contact:
    contact = db.scalar(
        select(Contact).where(
            Contact.id == contact_id,
            Contact.company_id == company_id,
            Contact.organization_id == organization_id,
        )
    )
    if contact is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Contact not found for this company.",
        )
    return contact


@router.get(
    "/{company_id}/contacts",
    response_model=list[ContactResponse],
)
def list_company_contacts(
    company_id: UUID,
    context: CurrentContext,
    db: DatabaseSession,
) -> list[Contact]:
    _get_company_for_contact_management(
        company_id,
        context.organization_id,
        db,
    )
    return list(
        db.scalars(
            select(Contact)
            .where(
                Contact.company_id == company_id,
                Contact.organization_id == context.organization_id,
            )
            .order_by(Contact.id)
        ).all()
    )


@router.post(
    "/{company_id}/contacts",
    response_model=ContactResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_company_contact(
    company_id: UUID,
    payload: ContactCreate,
    context: CurrentContext,
    db: DatabaseSession,
) -> Contact:
    _get_company_for_contact_management(
        company_id,
        context.organization_id,
        db,
    )
    contact = Contact(
        company_id=company_id,
        **payload.model_dump(),
    )
    db.add(contact)
    db.commit()
    db.refresh(contact)
    return contact


@router.get(
    "/{company_id}/contacts/{contact_id}",
    response_model=ContactResponse,
)
def get_company_contact(
    company_id: UUID,
    contact_id: UUID,
    context: CurrentContext,
    db: DatabaseSession,
) -> Contact:
    _get_company_for_contact_management(
        company_id,
        context.organization_id,
        db,
    )
    return _get_contact_or_404(
        company_id,
        contact_id,
        context.organization_id,
        db,
    )


@router.patch(
    "/{company_id}/contacts/{contact_id}",
    response_model=ContactResponse,
)
def update_company_contact(
    company_id: UUID,
    contact_id: UUID,
    payload: ContactUpdate,
    context: CurrentContext,
    db: DatabaseSession,
) -> Contact:
    _get_company_for_contact_management(
        company_id,
        context.organization_id,
        db,
    )
    contact = _get_contact_or_404(
        company_id,
        contact_id,
        context.organization_id,
        db,
    )
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(contact, field, value)
    db.commit()
    db.refresh(contact)
    return contact


@router.delete(
    "/{company_id}/contacts/{contact_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_company_contact(
    company_id: UUID,
    contact_id: UUID,
    context: CurrentContext,
    db: DatabaseSession,
) -> Response:
    _get_company_for_contact_management(
        company_id,
        context.organization_id,
        db,
    )
    contact = _get_contact_or_404(
        company_id,
        contact_id,
        context.organization_id,
        db,
    )
    db.delete(contact)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


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
        db.flush()
        website_url = _company_website_url(company)
        if website_url:
            upsert_website(
                db,
                website_url,
                discovery_source="company_profile",
            )
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


@router.get("/pipeline", response_model=CompanyPipelineResponse)
def list_company_pipeline(
    db: DatabaseSession,
    view: Literal["companies", "leads"] = "companies",
    query: Annotated[str, Query(max_length=300)] = "",
    status_filter: Annotated[
        str | None,
        Query(max_length=30, alias="status"),
    ] = None,
    email_status: Literal["all", "contacted", "not_contacted"] = "all",
    website: Literal["all", "with_website", "without_website"] = "all",
    page: int = Query(default=1, ge=1),
    page_size: Annotated[int, Query(ge=10, le=50)] = 10,
) -> CompanyPipelineResponse:
    if page_size not in (10, 25, 50):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="page_size must be 10, 25, or 50.",
        )
    return get_company_pipeline(
        db,
        view=view,
        query=query,
        status_filter=status_filter,
        email_status=email_status,
        website=website,
        page=page,
        page_size=page_size,
    )


@router.get(
    "/pipeline/selection",
    response_model=CompanyPipelineSelectionResponse,
)
def select_company_pipeline_results(
    db: DatabaseSession,
    view: Literal["companies", "leads"] = "leads",
    query: Annotated[str, Query(max_length=300)] = "",
    status_filter: Annotated[
        str | None,
        Query(max_length=30, alias="status"),
    ] = None,
    email_status: Literal["all", "contacted", "not_contacted"] = "all",
    website: Literal["all", "with_website", "without_website"] = "all",
) -> CompanyPipelineSelectionResponse:
    return get_company_pipeline_selection(
        db,
        view=view,
        query=query,
        status_filter=status_filter,
        email_status=email_status,
        website=website,
    )


@router.get(
    "/navigation-summary",
    response_model=CompanyNavigationSummaryResponse,
)
def company_navigation_summary(
    db: DatabaseSession,
) -> CompanyNavigationSummaryResponse:
    has_contacts = exists(
        select(Contact.id).where(Contact.company_id == Company.id)
    )
    count = db.scalar(
        select(func.count(Company.id)).where(
            Company.lead_status == "needs_review",
            has_contacts,
        )
    )
    return CompanyNavigationSummaryResponse(
        leads_needing_review=count or 0,
    )


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
    response_model=WebsiteCrawlJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def crawl_company_website(
    company_id: UUID,
    db: DatabaseSession,
) -> WebsiteCrawlJobResponse:
    company = await run_in_threadpool(db.get, Company, company_id)
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

    job = await create_website_crawl_job(
        website_url,
        db,
        discovery_source="company_profile",
    )
    return await run_in_threadpool(get_website_crawl_job, job.id, db)


@router.get(
    "/{company_id}/pagespeed-scans/{scan_id}",
    response_model=PageSpeedScanSummary,
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


@router.get(
    "/{company_id}/pagespeed-jobs/{job_id}",
    response_model=PageSpeedScanJobResponse,
)
def get_company_pagespeed_job(
    company_id: UUID,
    job_id: UUID,
    db: DatabaseSession,
) -> PageSpeedScanJobResponse:
    if db.get(Company, company_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Company not found.",
        )

    return get_pagespeed_scan_job(company_id, job_id, db)


@router.post(
    "/{company_id}/pagespeed-scans",
    response_model=PageSpeedScanSummary,
    status_code=status.HTTP_201_CREATED,
)
async def create_pagespeed_scan(
    company_id: UUID,
    payload: PageSpeedScanRequest,
    db: DatabaseSession,
) -> PageSpeedScan:
    company = await run_in_threadpool(db.get, Company, company_id)
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
        api_key = await run_in_threadpool(
            get_user_provider_api_key,
            db,
            "pagespeed",
        )
        result = await scan_pagespeed(website_url, payload.strategy, api_key)
    except PageSpeedError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc

    return await run_in_threadpool(
        _persist_pagespeed_scan,
        company.id,
        result,
        db,
    )


def _persist_pagespeed_scan(
    company_id: UUID,
    result: PageSpeedScanData,
    db: Session,
) -> PageSpeedScan:
    scan = PageSpeedScan(
        company_id=company_id,
        website_url=result.website_url,
        strategy=result.strategy,
        category_scores=result.category_scores,
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
        if "website" in company_data or "domain" in company_data:
            website_url = _company_website_url(company)
        else:
            website_url = None
        if website_url:
            upsert_website(
                db,
                website_url,
                discovery_source="company_profile",
            )
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
