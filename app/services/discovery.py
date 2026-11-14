import asyncio
from dataclasses import dataclass
from typing import Protocol

import httpx
from fastapi import HTTPException, status
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.schemas.discovery import (
    DiscoveryProvider,
    DiscoveryProviderFailure,
    DiscoveryResult,
    DiscoverySearchResponse,
)
from app.schemas.settings import TavilyUsageResponse
from app.services.application_settings import (
    get_selected_discovery_tools,
    get_tavily_api_key,
)
from app.services.octoparse import start_octoparse_search


TAVILY_SEARCH_URL = "https://api.tavily.com/search"
TAVILY_USAGE_URL = "https://api.tavily.com/usage"


async def search_companies(
    query: str,
    db: Session,
    max_results: int = 20,
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
                    provider=DiscoveryProvider.TAVILY,
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


@dataclass(frozen=True)
class StartedProviderJob:
    provider: DiscoveryProvider
    external_task_id: str
    lot_no: str | None
    max_results: int
    result_mapping: dict[str, str | None]


@dataclass(frozen=True)
class ProviderSearchOutcome:
    results: list[DiscoveryResult]
    job: StartedProviderJob | None = None


class LeadDiscoveryTool(Protocol):
    provider: DiscoveryProvider

    async def search(
        self,
        query: str,
        max_results: int,
        db: Session,
    ) -> ProviderSearchOutcome: ...


class TavilyDiscoveryTool:
    provider = DiscoveryProvider.TAVILY

    async def search(
        self,
        query: str,
        max_results: int,
        db: Session,
    ) -> ProviderSearchOutcome:
        response = await search_companies(query, db, max_results)
        return ProviderSearchOutcome(results=response.results)


class OctoparseDiscoveryTool:
    provider = DiscoveryProvider.OCTOPARSE

    async def search(
        self,
        query: str,
        max_results: int,
        db: Session,
    ) -> ProviderSearchOutcome:
        task_id, lot_no, result_mapping = await start_octoparse_search(
            query,
            max_results,
            db,
        )
        return ProviderSearchOutcome(
            results=[],
            job=StartedProviderJob(
                provider=self.provider,
                external_task_id=task_id,
                lot_no=lot_no,
                max_results=max_results,
                result_mapping=result_mapping,
            ),
        )


LEAD_DISCOVERY_TOOLS: dict[DiscoveryProvider, LeadDiscoveryTool] = {
    DiscoveryProvider.TAVILY: TavilyDiscoveryTool(),
    DiscoveryProvider.OCTOPARSE: OctoparseDiscoveryTool(),
}


async def search_with_selected_tools(
    query: str,
    max_results: int,
    db: Session,
) -> tuple[list[DiscoveryResult], list[StartedProviderJob], list[DiscoveryProviderFailure]]:
    selected_tools = get_selected_discovery_tools(db)

    async def run_tool(
        provider: DiscoveryProvider,
    ) -> tuple[
        DiscoveryProvider,
        ProviderSearchOutcome | None,
        DiscoveryProviderFailure | None,
    ]:
        try:
            outcome = await LEAD_DISCOVERY_TOOLS[provider].search(
                query,
                max_results,
                db,
            )
        except HTTPException as exc:
            return (
                provider,
                None,
                DiscoveryProviderFailure(
                    provider=provider,
                    detail=str(exc.detail),
                ),
            )
        return provider, outcome, None

    outcomes = await asyncio.gather(
        *(run_tool(provider) for provider in selected_tools)
    )
    results: list[DiscoveryResult] = []
    jobs: list[StartedProviderJob] = []
    failures: list[DiscoveryProviderFailure] = []
    for _, outcome, failure in outcomes:
        if failure is not None:
            failures.append(failure)
        elif outcome is not None:
            results.extend(outcome.results)
            if outcome.job is not None:
                jobs.append(outcome.job)

    if failures and not results and not jobs:
        detail = "; ".join(
            f"{failure.provider.value}: {failure.detail}"
            for failure in failures
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"All selected discovery tools failed. {detail}",
        )

    return results, jobs, failures


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
