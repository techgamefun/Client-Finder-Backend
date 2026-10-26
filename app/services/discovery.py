import httpx
from fastapi import HTTPException, status

from app.config import settings
from app.schemas.discovery import (
    DiscoveryResult,
    DiscoverySearchResponse,
)


TAVILY_SEARCH_URL = "https://api.tavily.com/search"


async def search_companies(
    query: str,
    max_results: int = 5,
) -> DiscoverySearchResponse:
    if not settings.tavily_api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Search provider is not configured.",
        )

    payload = {
        "api_key": settings.tavily_api_key,
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
