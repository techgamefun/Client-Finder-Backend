from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    PageSpeedScan,
    Website,
    WebsiteHypeStatProfile,
)
from app.schemas.website import WebsiteEnrichmentCountsResponse
from app.services.pagespeed import PAGESPEED_STRATEGIES


def get_website_enrichment_counts(
    db: Session,
) -> WebsiteEnrichmentCountsResponse:
    websites = db.scalars(select(Website)).all()
    if not websites:
        return WebsiteEnrichmentCountsResponse(
            classification_missing_count=0,
            ahrefs_missing_count=0,
            hypestat_missing_count=0,
            pagespeed_missing_scan_count=0,
        )

    website_ids = [website.id for website in websites]
    profiles = {
        profile.website_id: profile
        for profile in db.scalars(
            select(WebsiteHypeStatProfile).where(
                WebsiteHypeStatProfile.website_id.in_(website_ids)
            )
        ).all()
    }
    saved_scans = set(
        db.execute(
            select(PageSpeedScan.website_id, PageSpeedScan.strategy).where(
                PageSpeedScan.website_id.in_(website_ids)
            )
        ).all()
    )

    return WebsiteEnrichmentCountsResponse(
        classification_missing_count=sum(
            website.crawl_id is not None
            and (not website.page_type or not website.site_topic)
            for website in websites
        ),
        ahrefs_missing_count=sum(
            website.ahrefs_domain_rating is None for website in websites
        ),
        hypestat_missing_count=sum(
            (
                profile := profiles.get(website.id)
            ) is None
            or profile.status != "succeeded"
            or profile.payload is None
            for website in websites
        ),
        pagespeed_missing_scan_count=sum(
            (website.id, strategy) not in saved_scans
            for website in websites
            for strategy in PAGESPEED_STRATEGIES
        ),
    )
