import asyncio
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
from app.schemas.discovery import DiscoveryDomainRatingResult
from app.schemas.settings import AhrefsUsageResponse
from app.schemas.website import WebsiteAhrefsMetricsResponse
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


class _AhrefsAccessDenied(Exception):
    def __init__(self, endpoint: str, status_code: int) -> None:
        self.endpoint = endpoint
        self.status_code = status_code
        super().__init__(
            f"Ahrefs rejected {endpoint} with HTTP {status_code}."
        )


async def get_ahrefs_usage(
    db: Session,
) -> AhrefsUsageResponse | None:
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
        return None
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
) -> WebsiteAhrefsMetricsResponse:
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
        return WebsiteAhrefsMetricsResponse.model_validate(cached_metrics)
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Ahrefs is not configured. Add your API key in Settings.",
        )

    target = f"https://{domain}"
    try:
        async with async_http_client(timeout=30.0) as client:
            try:
                rating_response = await _request_ahrefs(
                    client,
                    "public/domain-rating-free",
                    api_key,
                    {"target": target},
                    billable=False,
                )
            except _AhrefsAccessDenied:
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail=(
                        "Ahrefs rejected the API key for free Domain Rating."
                    ),
                ) from exc

            snapshot_date = now.date()
            common_params = {
                "date": snapshot_date.isoformat(),
                "target": target,
            }
            try:
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
            except _AhrefsAccessDenied:
                metrics_response = None

            backlinks_response = None
            if metrics_response is not None:
                try:
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
                except _AhrefsAccessDenied:
                    backlinks_response = None
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
        metrics = (
            _SiteMetricsResponse.model_validate(metrics_response[0])
            if metrics_response is not None
            else None
        )
        backlinks = (
            _BacklinksMetricsResponse.model_validate(backlinks_response[0])
            if backlinks_response is not None
            else None
        )
    except (ValidationError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Ahrefs returned an invalid website metrics response.",
        ) from exc

    paid_responses = [
        response
        for response in (metrics_response, backlinks_response)
        if response is not None
    ]
    costs = [response[1] for response in paid_responses]
    if not paid_responses:
        return await run_in_threadpool(
            _persist_website_domain_rating,
            website_id,
            rating.domain_rating.domain_rating,
            rating.domain_rating.license,
            now,
            db,
        )
    if all(cost is not None for cost in costs):
        units_used = sum(cost for cost in costs if cost is not None)
    else:
        units_used = None
    values = {
        "snapshot_date": snapshot_date,
        "domain_rating": rating.domain_rating.domain_rating,
        "domain_rating_license_url": rating.domain_rating.license,
        "organic_traffic": metrics.metrics.org_traffic if metrics else None,
        "organic_traffic_cost_cents": (
            metrics.metrics.org_cost if metrics else None
        ),
        "organic_keywords": metrics.metrics.org_keywords if metrics else None,
        "backlinks_live": backlinks.metrics.live if backlinks else None,
        "backlinks_all_time": backlinks.metrics.all_time if backlinks else None,
        "refdomains_live": (
            backlinks.metrics.live_refdomains if backlinks else None
        ),
        "refdomains_all_time": (
            backlinks.metrics.all_time_refdomains if backlinks else None
        ),
        "units_used": units_used,
    }
    metrics = await run_in_threadpool(
        _persist_website_metrics,
        website_id,
        values,
        db,
    )
    return WebsiteAhrefsMetricsResponse.model_validate(metrics)


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
        raise _AhrefsAccessDenied(endpoint, response.status_code)
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
    fetched_at = datetime.now(timezone.utc)
    metrics.fetched_at = fetched_at
    website = db.get(Website, website_id)
    if website is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Website not found.",
        )
    website.ahrefs_domain_rating = values["domain_rating"]
    website.ahrefs_domain_rating_license_url = values[
        "domain_rating_license_url"
    ]
    website.ahrefs_domain_rating_fetched_at = fetched_at
    db.commit()
    db.refresh(metrics)
    return metrics


def _persist_website_domain_rating(
    website_id: UUID,
    domain_rating: float,
    license_url: str,
    fetched_at: datetime,
    db: Session,
) -> WebsiteAhrefsMetricsResponse:
    website = db.get(Website, website_id)
    if website is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Website not found.",
        )
    website.ahrefs_domain_rating = domain_rating
    website.ahrefs_domain_rating_license_url = license_url
    website.ahrefs_domain_rating_fetched_at = fetched_at
    db.commit()
    return WebsiteAhrefsMetricsResponse(
        snapshot_date=fetched_at.date(),
        domain_rating=domain_rating,
        domain_rating_license_url=license_url,
        fetched_at=fetched_at,
    )


async def fetch_website_domain_rating(
    website_id: UUID,
    db: Session,
) -> WebsiteAhrefsMetricsResponse:
    domain, api_key, _ = await run_in_threadpool(
        _prepare_website_metrics_fetch,
        website_id,
        db,
    )
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Ahrefs is not configured. Add your API key in Settings.",
        )

    fetched_at = datetime.now(timezone.utc)
    try:
        async with async_http_client(timeout=30.0) as client:
            rating_response, _ = await _request_ahrefs(
                client,
                "public/domain-rating-free",
                api_key,
                {"target": f"https://{domain}"},
                billable=False,
            )
    except _AhrefsAccessDenied as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Ahrefs rejected the API key for free Domain Rating.",
        ) from exc
    except httpx.TimeoutException as exc:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="Ahrefs Domain Rating request timed out.",
        ) from exc
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not reach Ahrefs to retrieve Domain Rating.",
        ) from exc

    try:
        rating = _DomainRatingResponse.model_validate(rating_response)
    except (ValidationError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Ahrefs returned an invalid Domain Rating response.",
        ) from exc

    return await run_in_threadpool(
        _persist_website_domain_rating,
        website_id,
        rating.domain_rating.domain_rating,
        rating.domain_rating.license,
        fetched_at,
        db,
    )


async def fetch_discovery_domain_ratings(
    domains: list[str],
    db: Session,
) -> list[DiscoveryDomainRatingResult]:
    context = get_tenant_context() or db.info.get("tenant_context")
    if context is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication is required.",
        )

    unique_domains = list(dict.fromkeys(domains))
    cached_ratings = await run_in_threadpool(
        _get_cached_discovery_domain_ratings,
        unique_domains,
        context.organization_id,
        db,
    )
    missing_domains = [
        domain for domain in unique_domains if domain not in cached_ratings
    ]
    fetched_ratings: dict[str, DiscoveryDomainRatingResult] = {}
    if missing_domains:
        api_key = await run_in_threadpool(
            get_user_provider_api_key,
            db,
            "ahrefs",
        )
        if not api_key:
            fetched_ratings = {
                domain: DiscoveryDomainRatingResult(
                    domain=domain,
                    status="failed",
                    error=(
                        "Add your Ahrefs API key in Settings to fetch "
                        "Domain Rating."
                    ),
                )
                for domain in missing_domains
            }
        else:
            async with async_http_client(timeout=30.0) as client:
                semaphore = asyncio.Semaphore(4)

                async def fetch_one(
                    domain: str,
                ) -> DiscoveryDomainRatingResult:
                    async with semaphore:
                        try:
                            response, _ = await _request_ahrefs(
                                client,
                                "public/domain-rating-free",
                                api_key,
                                {"target": f"https://{domain}"},
                                billable=False,
                            )
                            rating = _DomainRatingResponse.model_validate(
                                response
                            )
                        except _AhrefsAccessDenied as exc:
                            return DiscoveryDomainRatingResult(
                                domain=domain,
                                status="failed",
                                error=(
                                    "Ahrefs rejected the API key for free "
                                    "Domain Rating."
                                ),
                            )
                        except HTTPException as exc:
                            detail = (
                                exc.detail
                                if isinstance(exc.detail, str)
                                else str(exc.detail)
                            )
                            return DiscoveryDomainRatingResult(
                                domain=domain,
                                status="failed",
                                error=detail,
                            )
                        except httpx.TimeoutException:
                            return DiscoveryDomainRatingResult(
                                domain=domain,
                                status="failed",
                                error="Ahrefs Domain Rating request timed out.",
                            )
                        except httpx.RequestError:
                            return DiscoveryDomainRatingResult(
                                domain=domain,
                                status="failed",
                                error=(
                                    "Could not reach Ahrefs to retrieve "
                                    "Domain Rating."
                                ),
                            )
                        except (ValidationError, TypeError, ValueError):
                            return DiscoveryDomainRatingResult(
                                domain=domain,
                                status="failed",
                                error="Ahrefs returned an invalid Domain Rating.",
                            )

                        return DiscoveryDomainRatingResult(
                            domain=domain,
                            status="succeeded",
                            domain_rating=rating.domain_rating.domain_rating,
                            license_url=rating.domain_rating.license,
                            fetched_at=datetime.now(timezone.utc),
                        )

                results = await asyncio.gather(
                    *(fetch_one(domain) for domain in missing_domains)
                )
            fetched_ratings = {
                result.domain: result
                for result in results
            }
            successful_ratings = {
                domain: result
                for domain, result in fetched_ratings.items()
                if result.status == "succeeded"
            }
            if successful_ratings:
                await run_in_threadpool(
                    _persist_discovery_domain_ratings,
                    successful_ratings,
                    context.organization_id,
                    db,
                )

    return [
        cached_ratings.get(domain) or fetched_ratings[domain]
        for domain in unique_domains
    ]


def _get_cached_discovery_domain_ratings(
    domains: list[str],
    organization_id: UUID,
    db: Session,
) -> dict[str, DiscoveryDomainRatingResult]:
    websites = db.scalars(
        select(Website).where(
            Website.organization_id == organization_id,
            Website.domain.in_(domains),
            Website.ahrefs_domain_rating.is_not(None),
        )
    ).all()
    return {
        website.domain: DiscoveryDomainRatingResult(
            domain=website.domain,
            status="succeeded",
            domain_rating=website.ahrefs_domain_rating,
            license_url=website.ahrefs_domain_rating_license_url,
            fetched_at=website.ahrefs_domain_rating_fetched_at,
        )
        for website in websites
    }


def _persist_discovery_domain_ratings(
    ratings: dict[str, DiscoveryDomainRatingResult],
    organization_id: UUID,
    db: Session,
) -> None:
    websites = db.scalars(
        select(Website).where(
            Website.organization_id == organization_id,
            Website.domain.in_(ratings),
            Website.ahrefs_domain_rating.is_(None),
        )
    ).all()
    for website in websites:
        rating = ratings[website.domain]
        website.ahrefs_domain_rating = rating.domain_rating
        website.ahrefs_domain_rating_license_url = rating.license_url
        website.ahrefs_domain_rating_fetched_at = rating.fetched_at
    if websites:
        db.commit()


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
