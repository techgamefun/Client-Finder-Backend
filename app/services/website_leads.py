from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Company, Website
from app.schemas.website import WebsiteAddToLeadsResponse
from app.services.normalization import normalize_company_name


QUERY_BATCH_SIZE = 500


def add_websites_to_leads(
    db: Session,
    website_ids: list[UUID],
) -> WebsiteAddToLeadsResponse:
    unique_ids = list(dict.fromkeys(website_ids))
    websites: dict[UUID, Website] = {}
    for offset in range(0, len(unique_ids), QUERY_BATCH_SIZE):
        website_batch = unique_ids[offset : offset + QUERY_BATCH_SIZE]
        websites.update(
            (website.id, website)
            for website in db.scalars(
                select(Website).where(Website.id.in_(website_batch))
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
    existing_domains: set[str] = set()
    for offset in range(0, len(domains), QUERY_BATCH_SIZE):
        domain_batch = domains[offset : offset + QUERY_BATCH_SIZE]
        existing_domains.update(
            domain
            for domain in db.scalars(
                select(Company.domain).where(Company.domain.in_(domain_batch))
            ).all()
            if domain is not None
        )

    created_count = 0
    already_existing_count = 0
    status_updated_count = 0
    for website in eligible_websites:
        if website.domain in existing_domains:
            already_existing_count += 1
        else:
            db.add(
                Company(
                    name=website.domain,
                    normalized_name=normalize_company_name(website.domain),
                    website=website.website_url,
                    domain=website.domain,
                    lead_status="new",
                )
            )
            existing_domains.add(website.domain)
            created_count += 1

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
    )
