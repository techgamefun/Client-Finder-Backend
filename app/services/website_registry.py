from typing import Literal
from uuid import UUID
from urllib.parse import urlencode

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Website
from app.services.normalization import normalize_domain


CRAWL_CATEGORY_PAGE_TYPES = {
    "directories_listings": "business_listing",
    "marketplace": "business_listing",
    "classifieds": "business_listing",
    "job_boards_career_portals": "business_listing",
    "real_estate_property_portals": "business_listing",
    "news_editorial": "news",
    "blog": "blog",
    "ecommerce_retail": "ecommerce",
    "deals_and_coupons": "ecommerce",
    "documentation_knowledge_base": "documentation",
    "api_and_developer_platform": "documentation",
    "help_centers_support_portals": "documentation",
    "wiki_and_encyclopedia": "documentation",
    "forums_communities": "forum",
    "question_and_answer": "forum",
    "media_video_podcast_hubs": "media",
    "video_platform": "media",
    "audio_and_podcast_platform": "media",
    "photo_and_image_gallery": "media",
    "saas_corporate_marketing": "business_site",
    "corporate_and_business_website": "business_site",
    "portfolio_and_personal_website": "business_site",
    "landing_page_and_lead_generation": "business_site",
}


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


def website_favicon_url(website_url: str) -> str:
    canonical_url = canonical_website_url(website_url)
    return "https://www.google.com/s2/favicons?" + urlencode(
        {"domain_url": canonical_url, "sz": "64"}
    )


def upsert_website(
    db: Session,
    website_url: str,
    *,
    discovery_source: str | None = None,
    discovery_provider: str | None = None,
    category: str | None = None,
    crawl_id: UUID | None = None,
    language: str | None = None,
    favicon_url: str | None = None,
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
    if favicon_url and not website.favicon_url:
        website.favicon_url = favicon_url
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
        if website.page_type == "business_listing":
            website.discovery_source = "listing_website"
        elif website.discovery_source == "listing_website":
            website.discovery_source = (
                website.discovery_provider or discovery_source or "manual"
            )
    if crawl_id is not None:
        website.crawl_id = crawl_id

    db.flush()
    return website


def update_websites_status(
    db: Session,
    website_ids: list[UUID],
    status: Literal["new", "lead", "checked"],
) -> tuple[int, int]:
    unique_ids = list(dict.fromkeys(website_ids))
    websites = db.scalars(
        select(Website).where(Website.id.in_(unique_ids))
    ).all()
    updated_count = 0
    for website in websites:
        if website.status != status:
            website.status = status
            updated_count += 1
    return updated_count, len(unique_ids) - len(websites)
