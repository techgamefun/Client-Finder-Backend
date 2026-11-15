from datetime import datetime, timezone
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models import Website, WebsiteHypeStatProfile
from app.services.hypestat import fetch_hypestat_profiles
from app.services.application_settings import get_hypestat_settings


async def refetch_website_hypestat_profile(
    website_id: UUID,
    db: Session,
) -> WebsiteHypeStatProfile:
    website = db.get(Website, website_id)
    if website is None:
        raise HTTPException(
            status_code=404,
            detail="Website not found.",
        )

    profile = db.get(WebsiteHypeStatProfile, website_id)
    if profile is None:
        profile = WebsiteHypeStatProfile(
            website_id=website_id,
            status="pending",
        )
        db.add(profile)
    else:
        profile.status = "pending"
        profile.error = None
    db.commit()

    hypestat_settings = get_hypestat_settings(db)
    results = await fetch_hypestat_profiles(
        [website.domain],
        hypestat_settings.api_key,
    )
    result = results.get(website.domain) or results.get(
        website.domain.casefold()
    )
    if result is None:
        profile.status = "failed"
        profile.payload = None
        profile.error = "HypeStat lookup returned no result for this website."
    else:
        profile.status = result.status
        profile.payload = result.payload
        profile.error = result.error

    checked_at = datetime.now(timezone.utc)
    profile.checked_at = checked_at
    profile.fetched_at = (
        checked_at if profile.status == "succeeded" else None
    )
    db.commit()
    return profile
