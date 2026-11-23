from typing import Literal
from uuid import UUID

from sqlalchemy import case, exists, func, or_, select
from sqlalchemy.orm import Session, selectinload
from sqlalchemy.sql.elements import ColumnElement

from app.models import Company, Contact, lead_batch_companies
from app.schemas.company import (
    CompanyPipelineResponse,
    CompanyPipelineSelectionResponse,
)


CompanyView = Literal["companies", "leads"]
EmailStatusFilter = Literal["all", "contacted", "not_contacted"]
WebsiteFilter = Literal["all", "with_website", "without_website"]

COMPANY_DETAILS = (
    selectinload(Company.created_by_user),
    selectinload(Company.contacts),
    selectinload(Company.pagespeed_scans),
    selectinload(Company.lead_batches),
)
MAX_CAMPAIGN_SELECTION = 5_000


def _search_expression(query: str) -> ColumnElement[bool] | None:
    term = query.strip()
    if not term:
        return None
    escaped = term.replace("/", "//").replace("%", "/%").replace("_", "/_")
    pattern = f"%{escaped}%"
    return or_(
        Company.name.ilike(pattern, escape="/"),
        Company.domain.ilike(pattern, escape="/"),
        Company.industry.ilike(pattern, escape="/"),
        Company.city.ilike(pattern, escape="/"),
        Company.state.ilike(pattern, escape="/"),
    )


def _base_conditions(view: CompanyView) -> list[ColumnElement[bool]]:
    if view == "leads":
        return [
            exists(
                select(Contact.id).where(Contact.company_id == Company.id),
            ),
        ]
    return []


def _filter_conditions(
    *,
    query: str,
    status_filter: str | None,
    email_status: EmailStatusFilter,
    website: WebsiteFilter,
    batch_id: UUID | None,
    ungrouped: bool,
) -> list[ColumnElement[bool]]:
    conditions: list[ColumnElement[bool]] = []
    search_expression = _search_expression(query)
    if search_expression is not None:
        conditions.append(search_expression)
    if status_filter is not None:
        conditions.append(Company.lead_status == status_filter)
    if email_status == "contacted":
        conditions.append(Company.last_email_sent_at.is_not(None))
    elif email_status == "not_contacted":
        conditions.append(Company.last_email_sent_at.is_(None))
    if website == "with_website":
        conditions.append(
            or_(Company.website.is_not(None), Company.domain.is_not(None)),
        )
    elif website == "without_website":
        conditions.append(
            Company.website.is_(None) & Company.domain.is_(None),
        )
    if batch_id is not None:
        conditions.append(
            exists(
                select(lead_batch_companies.c.company_id).where(
                    lead_batch_companies.c.company_id == Company.id,
                    lead_batch_companies.c.lead_batch_id == batch_id,
                )
            )
        )
    elif ungrouped:
        conditions.append(
            ~exists(
                select(lead_batch_companies.c.company_id).where(
                    lead_batch_companies.c.company_id == Company.id,
                )
            )
        )
    return conditions


def get_company_pipeline(
    db: Session,
    *,
    view: CompanyView,
    query: str,
    status_filter: str | None,
    email_status: EmailStatusFilter,
    website: WebsiteFilter,
    batch_id: UUID | None,
    ungrouped: bool,
    page: int,
    page_size: int,
) -> CompanyPipelineResponse:
    base_conditions = _base_conditions(view)
    filter_conditions = _filter_conditions(
        query=query,
        status_filter=status_filter,
        email_status=email_status,
        website=website,
        batch_id=batch_id,
        ungrouped=ungrouped,
    )
    all_conditions = [*base_conditions, *filter_conditions]

    status_rows = db.execute(
        select(
            Company.lead_status,
            func.count(Company.id),
            func.coalesce(
                func.sum(
                    case(
                        (
                            or_(
                                Company.website.is_not(None),
                                Company.domain.is_not(None),
                            ),
                            1,
                        ),
                        else_=0,
                    ),
                ),
                0,
            ),
            func.coalesce(
                func.sum(
                    case(
                        (Company.last_email_sent_at.is_not(None), 1),
                        else_=0,
                    ),
                ),
                0,
            ),
        )
        .where(*base_conditions)
        .group_by(Company.lead_status)
    ).all()
    status_counts: dict[str, int] = {}
    with_website_count = 0
    contacted_count = 0
    for status, count, websites, contacted in status_rows:
        status_counts[status] = count
        with_website_count += websites
        contacted_count += contacted
    eligible_count = sum(status_counts.values())
    company_count = (
        eligible_count
        if view == "companies"
        else db.scalar(select(func.count(Company.id))) or 0
    )

    total_count = db.scalar(
        select(func.count(Company.id)).where(*all_conditions)
    ) or 0
    page_count = max(1, (total_count + page_size - 1) // page_size)
    effective_page = min(page, page_count)
    companies = db.scalars(
        select(Company)
        .options(*COMPANY_DETAILS)
        .where(*all_conditions)
        .order_by(Company.created_at.desc(), Company.id.desc())
        .limit(page_size)
        .offset((effective_page - 1) * page_size)
    ).all()

    return CompanyPipelineResponse(
        items=companies,
        page=effective_page,
        page_count=page_count,
        page_size=page_size,
        total_count=total_count,
        eligible_count=eligible_count,
        company_count=company_count,
        status_counts=status_counts,
        with_website_count=with_website_count,
        contacted_count=contacted_count,
    )


def get_company_pipeline_selection(
    db: Session,
    *,
    view: CompanyView,
    query: str,
    status_filter: str | None,
    email_status: EmailStatusFilter,
    website: WebsiteFilter,
    batch_id: UUID | None,
    ungrouped: bool,
) -> CompanyPipelineSelectionResponse:
    conditions = [
        *_base_conditions(view),
        *_filter_conditions(
            query=query,
            status_filter=status_filter,
            email_status=email_status,
            website=website,
            batch_id=batch_id,
            ungrouped=ungrouped,
        ),
    ]
    total_count = db.scalar(
        select(func.count(Company.id)).where(*conditions)
    ) or 0
    company_ids = db.scalars(
        select(Company.id)
        .where(*conditions)
        .order_by(Company.created_at.desc(), Company.id.desc())
        .limit(MAX_CAMPAIGN_SELECTION + 1)
    ).all()
    return CompanyPipelineSelectionResponse(
        company_ids=list(company_ids),
        total_count=total_count,
    )
