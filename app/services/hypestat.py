import logging
from dataclasses import dataclass
from typing import Any, Literal

import httpx

from app.config import settings


logger = logging.getLogger(__name__)

_BATCH_SIZE = 50
_STATUSES = Literal[
    "succeeded",
    "queued",
    "failed",
    "invalid_domain",
    "removed",
]


@dataclass(frozen=True)
class HypeStatFetchResult:
    status: _STATUSES
    payload: dict[str, Any] | None = None
    error: str | None = None


async def fetch_hypestat_profiles(
    domains: list[str],
    api_key: str | None,
) -> dict[str, HypeStatFetchResult]:
    if not domains:
        return {}
    if not api_key:
        detail = "HypeStat API key is not configured in Settings."
        logger.warning("HypeStat lookup skipped: %s", detail)
        return _failed_results(domains, detail)
    if not settings.crawler_service_shared_token:
        detail = "Crawler service shared token is not configured."
        logger.warning("HypeStat lookup skipped: %s", detail)
        return _failed_results(domains, detail)

    service_url = (
        f"{settings.crawler_service_url.rstrip('/')}/hypestat/domains"
    )
    results: dict[str, HypeStatFetchResult] = {}
    async with httpx.AsyncClient(timeout=30.0) as client:
        for offset in range(0, len(domains), _BATCH_SIZE):
            batch = domains[offset : offset + _BATCH_SIZE]
            try:
                response = await client.post(
                    service_url,
                    json={"domains": batch},
                    headers={
                        "X-HypeStat-API-Key": api_key,
                        "X-Crawler-Service-Token": (
                            settings.crawler_service_shared_token
                        ),
                    },
                )
            except httpx.TimeoutException:
                detail = "Crawler service timed out during HypeStat lookup."
                logger.warning(
                    "HypeStat lookup failed for %d domains: %s",
                    len(batch),
                    detail,
                )
                results.update(_failed_results(batch, detail))
                continue
            except httpx.RequestError as exc:
                detail = "Crawler service is unavailable for HypeStat lookup."
                logger.warning(
                    "HypeStat lookup failed for %d domains: %s (%s)",
                    len(batch),
                    detail,
                    exc,
                )
                results.update(_failed_results(batch, detail))
                continue

            try:
                body = response.json()
            except ValueError:
                detail = "Crawler service returned invalid HypeStat JSON."
                logger.warning(
                    "HypeStat lookup failed for %d domains: %s",
                    len(batch),
                    detail,
                )
                results.update(_failed_results(batch, detail))
                continue

            if response.is_error:
                detail = _response_error(response.status_code, body)
                logger.warning(
                    "HypeStat lookup failed for %d domains: %s",
                    len(batch),
                    detail,
                )
                results.update(_failed_results(batch, detail))
                continue

            if not isinstance(body, dict):
                detail = "Crawler service returned an invalid HypeStat response."
                logger.warning(
                    "HypeStat lookup failed for %d domains: %s",
                    len(batch),
                    detail,
                )
                results.update(_failed_results(batch, detail))
                continue

            data = body.get("data")
            entries = data.get("domains") if isinstance(data, dict) else None
            if not isinstance(entries, list):
                detail = "Crawler service returned an invalid HypeStat response."
                logger.warning(
                    "HypeStat lookup failed for %d domains: %s",
                    len(batch),
                    detail,
                )
                results.update(_failed_results(batch, detail))
                continue

            meta = body.get("meta")
            meta = meta if isinstance(meta, dict) else None
            results.update(_parse_domain_results(batch, entries, meta))

    return results


def _parse_domain_results(
    requested_domains: list[str],
    entries: list[Any],
    meta: dict[str, Any] | None,
) -> dict[str, HypeStatFetchResult]:
    requested = {domain.casefold() for domain in requested_domains}
    parsed: dict[str, HypeStatFetchResult] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        domain = entry.get("domain")
        status = entry.get("status")
        if not isinstance(domain, str) or domain.casefold() not in requested:
            continue

        payload = {"entry": entry, "meta": meta}
        message = entry.get("message")
        message = message if isinstance(message, str) else None
        if status == "ok" and isinstance(entry.get("data"), dict):
            parsed[domain.casefold()] = HypeStatFetchResult(
                status="succeeded",
                payload=payload,
            )
        elif status == "not_analyzed_yet":
            parsed[domain.casefold()] = HypeStatFetchResult(
                status="queued",
                payload=payload,
            )
        elif status == "invalid_domain":
            parsed[domain.casefold()] = HypeStatFetchResult(
                status="invalid_domain",
                payload=payload,
                error=message or "HypeStat rejected this domain.",
            )
        elif status == "removed":
            parsed[domain.casefold()] = HypeStatFetchResult(
                status="removed",
                payload=payload,
                error=message or "This website was removed from HypeStat.",
            )
        else:
            parsed[domain.casefold()] = HypeStatFetchResult(
                status="failed",
                payload=payload,
                error=message or "HypeStat returned an unsupported domain status.",
            )

    for domain in requested_domains:
        parsed.setdefault(
            domain.casefold(),
            HypeStatFetchResult(
                status="failed",
                error="HypeStat response omitted this domain.",
            ),
        )
    return parsed


def _response_error(status_code: int, body: Any) -> str:
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str) and message:
                return f"HypeStat returned HTTP {status_code}: {message}"
        detail = body.get("detail")
        if isinstance(detail, str) and detail:
            return f"HypeStat request failed with HTTP {status_code}: {detail}"
    return f"HypeStat request failed with HTTP {status_code}."


def _failed_results(
    domains: list[str],
    detail: str,
) -> dict[str, HypeStatFetchResult]:
    return {
        domain: HypeStatFetchResult(status="failed", error=detail)
        for domain in domains
    }
