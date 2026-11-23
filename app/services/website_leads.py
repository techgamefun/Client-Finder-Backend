from datetime import datetime, timezone
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import Company, LeadBatch, Website
from app.schemas.website import WebsiteAddToLeadsResponse
from app.services.contact_import import add_crawl_contacts
from app.services.normalization import normalize_company_name


QUERY_BATCH_SIZE = 500


def add_websites_to_leads(
    db: Session,
    website_ids: list[UUID],
    batch_id: UUID | None = None,
    batch_name: str | None = None,
) -> WebsiteAddToLeadsResponse:
    unique_ids = list(dict.fromkeys(website_ids))
    websites: dict[UUID, Website] = {}
    for offset in range(0, len(unique_ids), QUERY_BATCH_SIZE):
        website_batch = unique_ids[offset : offset + QUERY_BATCH_SIZE]
        websites.update(
            (website.id, website)
            for website in db.scalars(
            select(Website)
            .options(selectinload(Website.crawl))
            .where(Website.id.in_(website_batch))
            ).all()
        )

    eligible_websites: list[Website] = []
    skipped_listing_count = 0
    skipped_uncrawled_count = 0
    for website in websites.values():
        if website.page_type == "business_listing":
            skipped_listing_count += 1
        elif website.crawl_id is None:
            skipped_uncrawled_count += 1
        else:
            eligible_websites.append(website)

    domains = list({website.domain for website in eligible_websites})
    existing_companies_by_domain: dict[str, Company] = {}
    for offset in range(0, len(domains), QUERY_BATCH_SIZE):
        domain_batch = domains[offset : offset + QUERY_BATCH_SIZE]
        companies = db.scalars(
            select(Company)
            .options(
                selectinload(Company.contacts),
                selectinload(Company.lead_batches),
            )
            .where(Company.domain.in_(domain_batch))
        ).all()
        existing_companies_by_domain.update(
            (company.domain, company)
            for company in companies
            if company.domain is not None
        )

    created_count = 0
    already_existing_count = 0
    status_updated_count = 0
    lead_batch = db.get(LeadBatch, batch_id) if batch_id else None
    if batch_id is not None and lead_batch is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Lead batch not found.",
        )
    if eligible_websites and lead_batch is None:
        created_at = datetime.now(timezone.utc)
        lead_batch = LeadBatch(
            name=batch_name
            or f"Website import · {created_at.strftime('%Y-%m-%d %H:%M UTC')}",
        )
        db.add(lead_batch)
        db.flush()

    for website in eligible_websites:
        company = existing_companies_by_domain.get(website.domain)
        if company is not None:
            already_existing_count += 1
        else:
            company = Company(
                name=website.domain,
                normalized_name=normalize_company_name(website.domain),
                website=website.website_url,
                domain=website.domain,
                lead_status="new",
            )
            db.add(company)
            existing_companies_by_domain[website.domain] = company
            created_count += 1

        if lead_batch is not None and all(
            existing_batch.id != lead_batch.id
            for existing_batch in company.lead_batches
        ):
            company.lead_batches.append(lead_batch)

        if website.crawl is not None:
            add_crawl_contacts(
                company,
                website.crawl.emails,
                website.crawl.phones,
            )

        if website.status != "lead":
            status_updated_count += 1
            website.status = "lead"

    return WebsiteAddToLeadsResponse(
        requested_count=len(unique_ids),
        created_count=created_count,
        already_existing_count=already_existing_count,
        status_updated_count=status_updated_count,
        skipped_listing_count=skipped_listing_count,
        skipped_uncrawled_count=skipped_uncrawled_count,
        not_found_count=len(unique_ids) - len(websites),
        batch_id=lead_batch.id if lead_batch is not None else None,
        batch_name=lead_batch.name if lead_batch is not None else None,
    )
