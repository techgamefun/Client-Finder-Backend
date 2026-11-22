import httpx
from fastapi import HTTPException, status
from pydantic import ValidationError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.http_clients import async_http_client
from app.schemas.settings import HypeStatUsageResponse
from app.services.application_settings import get_user_provider_api_key


HYPESTAT_USAGE_URL = "https://api.hypestat.com/v1/usage"


async def get_hypestat_usage(db: Session) -> HypeStatUsageResponse:
    api_key = await run_in_threadpool(
        get_user_provider_api_key,
        db,
        "hypestat",
    )
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="HypeStat is not configured.",
        )

    try:
        async with async_http_client(timeout=20.0) as client:
            response = await client.get(
                HYPESTAT_USAGE_URL,
                headers={"Authorization": f"Bearer {api_key}"},
            )
    except httpx.TimeoutException as exc:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="HypeStat usage request timed out.",
        ) from exc
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not reach HypeStat to retrieve usage.",
        ) from exc

    if response.status_code in (401, 403):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="HypeStat rejected the configured API key.",
        )
    if response.status_code == 429:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="HypeStat rate-limited the usage request. Try again later.",
        )
    if response.is_error:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                "HypeStat usage request failed with HTTP "
                f"{response.status_code}."
            ),
        )

    try:
        body = response.json()
        return HypeStatUsageResponse.model_validate(body)
    except (ValidationError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="HypeStat returned an invalid usage response.",
        ) from exc
