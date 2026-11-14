from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Website
from app.services.normalization import normalize_domain


LISTING_CATEGORY = "directories_listings"

CRAWL_CATEGORY_PAGE_TYPES = {
    "directories_listings": "business_listing",
    "news_editorial": "news",
    "ecommerce_retail": "ecommerce",
    "documentation_knowledge_base": "documentation",
    "forums_communities": "forum",
    "media_video_podcast_hubs": "media",
}


def is_listing_category(category: str | None) -> bool:
    return category == LISTING_CATEGORY


def page_type_from_crawl_category(category: str | None) -> str | None:
    if category is None:
        return None
    return CRAWL_CATEGORY_PAGE_TYPES.get(category)


def canonical_website_url(value: str) -> str:
    domain = normalize_domain(value)
    if domain is None:
        raise ValueError(f"Could not determine a website domain from {value!r}.")
    host = f"[{domain}]" if ":" in domain else domain
    return f"https://{host}/"


def upsert_website(
    db: Session,
    website_url: str,
    *,
    discovery_source: str | None = None,
    discovery_provider: str | None = None,
    category: str | None = None,
    crawl_id: UUID | None = None,
    language: str | None = None,
) -> Website:
    domain = normalize_domain(website_url)
    if domain is None:
        raise ValueError(f"Could not determine a website domain from {website_url!r}.")
    canonical_url = canonical_website_url(website_url)
    website = db.scalar(
        select(Website)
        .where(Website.domain == domain)
        .limit(1)
    )
    if website is None:
        website = Website(
            website_url=canonical_url,
            domain=domain,
            discovery_source=discovery_source or "manual",
        )
        db.add(website)

    website.website_url = canonical_url
    website.domain = domain
    if language and not website.language:
        website.language = language.strip()[:35] or None
    if discovery_source and website.discovery_source in {
        "manual",
        "existing_data",
        "existing_crawl",
    }:
        website.discovery_source = discovery_source
    if discovery_provider and website.discovery_provider is None:
        website.discovery_provider = discovery_provider
    if category is not None:
        website.category = category
        if not website.classification_manual:
            inferred_page_type = page_type_from_crawl_category(category)
            if inferred_page_type:
                website.page_type = inferred_page_type
        website.site_type = "listing" if (
            website.page_type == "business_listing"
            or (
                not website.classification_manual
                and is_listing_category(category)
            )
        ) else "normal"
        if website.site_type == "listing":
            website.discovery_source = "listing_website"
        elif website.discovery_source == "listing_website":
            website.discovery_source = (
                website.discovery_provider or discovery_source or "manual"
            )
    if crawl_id is not None:
        website.crawl_id = crawl_id

    db.flush()
    return website
