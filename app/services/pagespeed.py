import math
from dataclasses import dataclass
from typing import Any, Literal

import httpx
from fastapi import HTTPException

from app.config import settings
from app.services.website_crawler import _validate_public_url


PAGESPEED_API_URL = (
    "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
)
LIGHTHOUSE_CATEGORIES = (
    "performance",
    "accessibility",
    "best-practices",
    "seo",
)
PageSpeedStrategy = Literal["mobile", "desktop"]
PAGESPEED_STRATEGIES: tuple[PageSpeedStrategy, ...] = ("mobile", "desktop")


class PageSpeedError(Exception):
    pass


@dataclass(frozen=True)
class PageSpeedScanData:
    website_url: str
    strategy: PageSpeedStrategy
    category_scores: dict[str, int | None]
    report: dict[str, Any]


async def scan_pagespeed(
    website_url: str,
    strategy: PageSpeedStrategy,
) -> PageSpeedScanData:
    if not settings.google_pagespeed_api_key:
        raise PageSpeedError("Google PageSpeed API key is not configured.")

    try:
        await _validate_public_url(website_url)
    except HTTPException as exc:
        raise PageSpeedError(str(exc.detail)) from exc

    params: dict[str, str | list[str]] = {
        "url": website_url,
        "key": settings.google_pagespeed_api_key,
        "strategy": strategy,
        "category": list(LIGHTHOUSE_CATEGORIES),
    }

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.get(PAGESPEED_API_URL, params=params)
            response.raise_for_status()
    except httpx.TimeoutException as exc:
        raise PageSpeedError("Google PageSpeed scan timed out.") from exc
    except httpx.HTTPStatusError as exc:
        raise PageSpeedError(
            "Google PageSpeed rejected the scan request."
        ) from exc
    except httpx.RequestError as exc:
        raise PageSpeedError(
            "Could not reach Google PageSpeed."
        ) from exc

    try:
        data = response.json()
    except ValueError as exc:
        raise PageSpeedError(
            "Google PageSpeed returned an invalid report."
        ) from exc

    if not isinstance(data, dict):
        raise PageSpeedError("Google PageSpeed returned an invalid report.")

    lighthouse_result = data.get("lighthouseResult")
    if not isinstance(lighthouse_result, dict):
        raise PageSpeedError(
            "Google PageSpeed did not return Lighthouse results."
        )

    categories = lighthouse_result.get("categories")
    if not isinstance(categories, dict):
        raise PageSpeedError(
            "Google PageSpeed returned no category scores."
        )

    category_scores: dict[str, int | None] = {}
    for category in LIGHTHOUSE_CATEGORIES:
        category_data = categories.get(category)
        score = (
            category_data.get("score")
            if isinstance(category_data, dict)
            else None
        )
        category_scores[category] = (
            round(score * 100)
            if (
                isinstance(score, (int, float))
                and not isinstance(score, bool)
                and math.isfinite(score)
                and 0 <= score <= 1
            )
            else None
        )

    return PageSpeedScanData(
        website_url=website_url,
        strategy=strategy,
        category_scores=category_scores,
        report={
            "analysisUTCTimestamp": data.get("analysisUTCTimestamp"),
            "loadingExperience": data.get("loadingExperience"),
            "originLoadingExperience": data.get("originLoadingExperience"),
            "lighthouseResult": lighthouse_result,
        },
    )
