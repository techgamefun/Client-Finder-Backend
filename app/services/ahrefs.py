from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import httpx
from fastapi import HTTPException, status
from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.http_clients import async_http_client
from app.models import Website, WebsiteAhrefsMetrics
from app.schemas.settings import AhrefsUsageResponse
from app.services.application_settings import get_user_provider_api_key
from app.tenant_context import get_tenant_context


AHREFS_API_BASE_URL = "https://api.ahrefs.com/v3"
AHREFS_CACHE_TTL = timedelta(hours=24)


class _DomainRating(BaseModel):
    domain_rating: float
    license: str


class _DomainRatingResponse(BaseModel):
    domain_rating: _DomainRating


class _SiteMetrics(BaseModel):
    org_traffic: int | None
    org_cost: int | None
    org_keywords: int | None


class _SiteMetricsResponse(BaseModel):
    metrics: _SiteMetrics


class _BacklinksMetrics(BaseModel):
    live: int | None
    all_time: int | None
    live_refdomains: int | None
    all_time_refdomains: int | None


class _BacklinksMetricsResponse(BaseModel):
    metrics: _BacklinksMetrics


async def get_ahrefs_usage(db: Session) -> AhrefsUsageResponse:
    api_key = await run_in_threadpool(
        get_user_provider_api_key,
        db,
        "ahrefs",
    )
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Ahrefs is not configured.",
        )

    try:
        async with async_http_client(timeout=20.0) as client:
            response = await client.get(
                f"{AHREFS_API_BASE_URL}/subscription-info/limits-and-usage",
                params={"output": "json"},
                headers={"Authorization": f"Bearer {api_key}"},
            )
    except httpx.TimeoutException as exc:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="Ahrefs usage request timed out.",
        ) from exc
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not reach Ahrefs to retrieve usage.",
        ) from exc

    if response.status_code in (401, 403):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Ahrefs rejected the configured API key.",
        )
    if response.status_code == 429:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Ahrefs rate-limited the usage request. Try again later.",
        )
    if response.is_error:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                "Ahrefs usage request failed with HTTP "
                f"{response.status_code}."
            ),
        )

    try:
        return AhrefsUsageResponse.model_validate(response.json())
    except (ValidationError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Ahrefs returned an invalid usage response.",
        ) from exc


async def fetch_website_ahrefs_metrics(
    website_id: UUID,
    db: Session,
) -> WebsiteAhrefsMetrics:
    domain, api_key, cached_metrics = await run_in_threadpool(
        _prepare_website_metrics_fetch,
        website_id,
        db,
    )
    now = datetime.now(timezone.utc)
    if (
        cached_metrics is not None
        and _as_utc(cached_metrics.fetched_at) >= now - AHREFS_CACHE_TTL
    ):
        return cached_metrics
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Ahrefs is not configured. Add your API key in Settings.",
        )

    target = f"https://{domain}"
    snapshot_date = now.date()
    common_params = {"date": snapshot_date.isoformat(), "target": target}
    try:
        async with async_http_client(timeout=30.0) as client:
            rating_response = await _request_ahrefs(
                client,
                "public/domain-rating-free",
                api_key,
                {"target": target},
                billable=False,
            )
            metrics_response = await _request_ahrefs(
                client,
                "site-explorer/metrics",
                api_key,
                {
                    **common_params,
                    "mode": "subdomains",
                    "output": "json",
                },
                billable=True,
            )
            backlinks_response = await _request_ahrefs(
                client,
                "site-explorer/backlinks-stats",
                api_key,
                {
                    **common_params,
                    "mode": "subdomains",
                    "output": "json",
                },
                billable=True,
            )
    except httpx.TimeoutException as exc:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="Ahrefs metrics request timed out.",
        ) from exc
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not reach Ahrefs to retrieve website metrics.",
        ) from exc

    try:
        rating = _DomainRatingResponse.model_validate(rating_response[0])
        metrics = _SiteMetricsResponse.model_validate(metrics_response[0])
        backlinks = _BacklinksMetricsResponse.model_validate(
            backlinks_response[0]
        )
    except (ValidationError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Ahrefs returned an invalid website metrics response.",
        ) from exc

    costs = [metrics_response[1], backlinks_response[1]]
    units_used = sum(costs) if all(cost is not None for cost in costs) else None
    values = {
        "snapshot_date": snapshot_date,
        "domain_rating": rating.domain_rating.domain_rating,
        "domain_rating_license_url": rating.domain_rating.license,
        "organic_traffic": metrics.metrics.org_traffic,
        "organic_traffic_cost_cents": metrics.metrics.org_cost,
        "organic_keywords": metrics.metrics.org_keywords,
        "backlinks_live": backlinks.metrics.live,
        "backlinks_all_time": backlinks.metrics.all_time,
        "refdomains_live": backlinks.metrics.live_refdomains,
        "refdomains_all_time": backlinks.metrics.all_time_refdomains,
        "units_used": units_used,
    }
    return await run_in_threadpool(
        _persist_website_metrics,
        website_id,
        values,
        db,
    )


def get_cached_website_ahrefs_metrics(
    website_id: UUID,
    db: Session,
) -> WebsiteAhrefsMetrics | None:
    context = get_tenant_context() or db.info.get("tenant_context")
    if context is None:
        return None
    return db.scalar(
        select(WebsiteAhrefsMetrics).where(
            WebsiteAhrefsMetrics.website_id == website_id,
            WebsiteAhrefsMetrics.created_by_user_id == context.user_id,
        )
    )


async def _request_ahrefs(
    client: httpx.AsyncClient,
    endpoint: str,
    api_key: str,
    params: dict[str, str],
    *,
    billable: bool,
) -> tuple[dict[str, Any], int | None]:
    response = await client.get(
        f"{AHREFS_API_BASE_URL}/{endpoint}",
        params=params,
        headers={"Authorization": f"Bearer {api_key}"},
    )

    if response.status_code in (401, 403):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Ahrefs rejected the configured API key.",
        )
    if response.status_code == 429:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Ahrefs rate-limited the metrics request. Try again later.",
        )
    if response.is_error:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                f"Ahrefs {endpoint} request failed with HTTP "
                f"{response.status_code}."
            ),
        )

    try:
        body = response.json()
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Ahrefs returned invalid JSON for {endpoint}.",
        ) from exc
    if not isinstance(body, dict):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Ahrefs returned an invalid response for {endpoint}.",
        )

    units_used = None
    if billable:
        raw_units = response.headers.get("x-api-units-cost-total-actual")
        try:
            units_used = int(raw_units) if raw_units is not None else None
        except ValueError:
            units_used = None
    return body, units_used


def _prepare_website_metrics_fetch(
    website_id: UUID,
    db: Session,
) -> tuple[str, str | None, WebsiteAhrefsMetrics | None]:
    website = db.get(Website, website_id)
    if website is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Website not found.",
        )

    context = get_tenant_context() or db.info.get("tenant_context")
    if context is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication is required.",
        )
    cached_metrics = db.scalar(
        select(WebsiteAhrefsMetrics).where(
            WebsiteAhrefsMetrics.website_id == website_id,
            WebsiteAhrefsMetrics.created_by_user_id == context.user_id,
        )
    )
    api_key = get_user_provider_api_key(db, "ahrefs")
    return website.domain, api_key, cached_metrics


def _persist_website_metrics(
    website_id: UUID,
    values: dict[str, Any],
    db: Session,
) -> WebsiteAhrefsMetrics:
    context = get_tenant_context() or db.info.get("tenant_context")
    if context is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication is required.",
        )
    metrics = db.scalar(
        select(WebsiteAhrefsMetrics).where(
            WebsiteAhrefsMetrics.website_id == website_id,
            WebsiteAhrefsMetrics.created_by_user_id == context.user_id,
        )
    )
    if metrics is None:
        metrics = WebsiteAhrefsMetrics(
            website_id=website_id,
            **values,
        )
        db.add(metrics)
    else:
        for field, value in values.items():
            setattr(metrics, field, value)
        metrics.fetched_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(metrics)
    return metrics


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
