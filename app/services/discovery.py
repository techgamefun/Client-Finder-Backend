import httpx
from fastapi import HTTPException, status
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.schemas.discovery import (
    DiscoveryResult,
    DiscoverySearchResponse,
)
from app.schemas.settings import TavilyUsageResponse
from app.services.application_settings import get_tavily_api_key


TAVILY_SEARCH_URL = "https://api.tavily.com/search"
TAVILY_USAGE_URL = "https://api.tavily.com/usage"


async def search_companies(
    query: str,
    db: Session,
    max_results: int = 5,
) -> DiscoverySearchResponse:
    api_key = get_tavily_api_key(db)
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Search provider is not configured.",
        )

    payload = {
        "api_key": api_key,
        "query": query,
        "search_depth": "basic",
        "max_results": max_results,
        "include_answer": False,
    }

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(
                TAVILY_SEARCH_URL,
                json=payload,
            )
            response.raise_for_status()

    except httpx.TimeoutException as exc:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="Search provider timed out.",
        ) from exc

    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Search provider returned an error.",
        ) from exc

    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not reach the search provider.",
        ) from exc

    data = response.json()
    results = []

    for item in data.get("results", []):
        try:
            results.append(
                DiscoveryResult(
                    title=item["title"],
                    url=item["url"],
                    content=item.get("content", ""),
                    score=item.get("score", 0.0),
                )
            )
        except (KeyError, ValueError, TypeError):
            # Skip malformed results rather than returning
            # unvalidated source data.
            continue

    return DiscoverySearchResponse(
        query=query,
        results=results,
    )


async def get_tavily_usage(db: Session) -> TavilyUsageResponse:
    api_key = get_tavily_api_key(db)
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Tavily is not configured.",
        )

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.get(
                TAVILY_USAGE_URL,
                headers={"Authorization": f"Bearer {api_key}"},
            )
    except httpx.TimeoutException as exc:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="Tavily usage request timed out.",
        ) from exc
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not reach Tavily to retrieve usage.",
        ) from exc

    if response.status_code in (401, 403):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Tavily rejected the configured API key.",
        )
    if response.status_code == 429:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Tavily rate-limited the usage request. Try again later.",
        )
    if response.is_error:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Tavily usage request failed with HTTP {response.status_code}.",
        )

    try:
        return TavilyUsageResponse.model_validate(response.json())
    except (ValidationError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Tavily returned an invalid usage response.",
        ) from exc
