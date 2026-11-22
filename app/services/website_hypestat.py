from datetime import datetime, timezone
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.models import Website, WebsiteHypeStatProfile
from app.services.hypestat import HypeStatFetchResult, fetch_hypestat_profiles
from app.services.application_settings import get_hypestat_settings


async def refetch_website_hypestat_profile(
    website_id: UUID,
    db: Session,
) -> WebsiteHypeStatProfile:
    domain, api_key = await run_in_threadpool(
        _prepare_hypestat_refetch,
        website_id,
        db,
    )
    results = await fetch_hypestat_profiles([domain], api_key)
    result = results.get(domain) or results.get(domain.casefold())
    return await run_in_threadpool(
        _persist_hypestat_result,
        website_id,
        result,
        db,
    )


def _prepare_hypestat_refetch(
    website_id: UUID,
    db: Session,
) -> tuple[str, str | None]:
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
    return website.domain, hypestat_settings.api_key


def _persist_hypestat_result(
    website_id: UUID,
    result: HypeStatFetchResult | None,
    db: Session,
) -> WebsiteHypeStatProfile:
    profile = db.get(WebsiteHypeStatProfile, website_id)
    if profile is None:
        raise RuntimeError(
            f"HypeStat profile row disappeared for website {website_id}."
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
