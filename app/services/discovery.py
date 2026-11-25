import asyncio
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit

import httpx
from fastapi import HTTPException, status
from pydantic import ValidationError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.http_clients import async_http_client
from app.schemas.discovery import (
    DiscoveryProvider,
    DiscoveryProviderFailure,
    DiscoveryResult,
    DiscoverySearchResponse,
    TavilyDiscoverySearchRequest,
)
from app.schemas.settings import TavilyUsageResponse
from app.services.application_settings import (
    get_selected_discovery_tools,
    get_tavily_api_key,
)
from app.services.octoparse import (
    OctoparseSearchConfig,
    get_octoparse_search_config,
    start_octoparse_search,
)


TAVILY_SEARCH_URL = "https://api.tavily.com/search"
TAVILY_USAGE_URL = "https://api.tavily.com/usage"


async def search_companies(
    query: str,
    db: Session,
    max_results: int = 20,
    *,
    options: TavilyDiscoverySearchRequest | None = None,
    api_key: str | None = None,
    api_key_is_loaded: bool = False,
) -> DiscoverySearchResponse:
    if not api_key_is_loaded:
        api_key = await run_in_threadpool(get_tavily_api_key, db)
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Search provider is not configured.",
        )

    search_options = options or TavilyDiscoverySearchRequest(
        query=query,
        max_results=max_results,
    )
    payload: dict[str, object] = {
        "api_key": api_key,
        "query": query,
        "search_depth": search_options.search_depth,
        "topic": search_options.topic,
        "max_results": search_options.max_results,
        "include_answer": search_options.include_answer,
        "include_favicon": search_options.include_favicon,
        "auto_parameters": search_options.auto_parameters,
        "include_usage": True,
        "include_raw_content": False,
        "filter_by_published_date": search_options.filter_by_published_date,
    }
    if search_options.search_depth in {"basic", "fast", "advanced"}:
        payload["chunks_per_source"] = search_options.chunks_per_source
    if search_options.topic == "general" and search_options.country:
        payload["country"] = search_options.country
    if search_options.time_range:
        payload["time_range"] = search_options.time_range
    if search_options.start_date:
        payload["start_date"] = search_options.start_date.isoformat()
    if search_options.end_date:
        payload["end_date"] = search_options.end_date.isoformat()
    if search_options.include_domains:
        payload["include_domains"] = search_options.include_domains
    if search_options.include_domains_mode:
        payload["include_domains_mode"] = search_options.include_domains_mode
    if search_options.exclude_domains:
        payload["exclude_domains"] = search_options.exclude_domains

    try:
        async with async_http_client(timeout=20.0) as client:
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
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("title"), str)
            or not isinstance(item.get("url"), str)
        ):
            continue
        result_data = {
            "title": item["title"],
            "url": item["url"],
            "content": item.get("content", ""),
            "score": item.get("score", 0.0),
            "provider": DiscoveryProvider.TAVILY,
            "favicon": item.get("favicon"),
            "published_date": item.get("published_date"),
        }
        try:
            result = DiscoveryResult(**result_data)
        except (KeyError, ValueError, TypeError):
            if not item.get("favicon"):
                continue
            result_data["favicon"] = None
            try:
                result = DiscoveryResult(**result_data)
            except (KeyError, ValueError, TypeError):
                continue
        if (
            search_options.min_score is not None
            and result.score < search_options.min_score
        ):
            continue
        results.append(result)

    answer = data.get("answer") if search_options.include_answer else None
    if not isinstance(answer, str):
        answer = None
    usage = data.get("usage")
    credits_value = (
        usage.get("credits", usage.get("credits_used"))
        if isinstance(usage, dict)
        else None
    )
    if credits_value is None:
        credits_value = data.get("credits_used")
    credits_used = (
        float(credits_value)
        if isinstance(credits_value, (int, float))
        and not isinstance(credits_value, bool)
        else None
    )

    return DiscoverySearchResponse(
        query=query,
        results=results,
        answer=answer,
        credits_used=credits_used,
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
    answer: str | None = None
    credits_used: float | None = None


class LeadDiscoveryTool(Protocol):
    provider: DiscoveryProvider

    async def search(
        self,
        query: str,
        max_results: int,
        db: Session,
        options: TavilyDiscoverySearchRequest | None = None,
        provider_config: str | OctoparseSearchConfig | None = None,
    ) -> ProviderSearchOutcome: ...


class TavilyDiscoveryTool:
    provider = DiscoveryProvider.TAVILY

    async def search(
        self,
        query: str,
        max_results: int,
        db: Session,
        options: TavilyDiscoverySearchRequest | None = None,
        provider_config: str | OctoparseSearchConfig | None = None,
    ) -> ProviderSearchOutcome:
        response = await search_companies(
            query,
            db,
            max_results,
            options=options,
            api_key=(
                provider_config
                if isinstance(provider_config, str)
                else None
            ),
            api_key_is_loaded=True,
        )
        return ProviderSearchOutcome(
            results=response.results,
            answer=response.answer,
            credits_used=response.credits_used,
        )


class OctoparseDiscoveryTool:
    provider = DiscoveryProvider.OCTOPARSE

    async def search(
        self,
        query: str,
        max_results: int,
        db: Session,
        options: TavilyDiscoverySearchRequest | None = None,
        provider_config: str | OctoparseSearchConfig | None = None,
    ) -> ProviderSearchOutcome:
        task_id, lot_no, result_mapping = await start_octoparse_search(
            query,
            max_results,
            db,
            config=(
                provider_config
                if isinstance(provider_config, OctoparseSearchConfig)
                else None
            ),
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
    *,
    options: TavilyDiscoverySearchRequest | None = None,
) -> tuple[
    list[DiscoveryResult],
    list[StartedProviderJob],
    list[DiscoveryProviderFailure],
    str | None,
    float | None,
]:
    selected_tools, provider_configs, failures = (
        await _load_selected_discovery_tools(db, options)
    )
    results, jobs, failures, answer, credits_used = (
        await _search_with_preloaded_tools(
            query,
            max_results,
            db,
            options,
            selected_tools,
            provider_configs,
            failures,
        )
    )
    if failures and not results and not jobs:
        detail = "; ".join(
            f"{failure.provider.value}: {failure.detail}"
            for failure in failures
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"All selected discovery tools failed. {detail}",
        )
    return results, jobs, failures, answer, credits_used


async def search_multiple_with_selected_tools(
    queries: list[str],
    max_results: int,
    db: Session,
    *,
    options: TavilyDiscoverySearchRequest,
) -> tuple[
    list[DiscoveryResult],
    list[StartedProviderJob],
    list[DiscoveryProviderFailure],
    str | None,
    float | None,
]:
    if not queries:
        raise ValueError("At least one discovery query is required.")
    selected_tools, provider_configs, setup_failures = (
        await _load_selected_discovery_tools(db, options)
    )
    result_counts = distribute_result_limit(max_results, len(queries))
    outcomes = await asyncio.gather(
        *(
            _search_with_preloaded_tools(
                query,
                result_count,
                db,
                options.model_copy(
                    update={"query": query, "max_results": result_count},
                ),
                selected_tools,
                provider_configs,
                setup_failures,
            )
            for query, result_count in zip(queries, result_counts, strict=True)
        )
    )

    results: list[DiscoveryResult] = []
    jobs: list[StartedProviderJob] = []
    failures: list[DiscoveryProviderFailure] = []
    answers: list[str] = []
    credits_used = 0.0
    for query, outcome in zip(queries, outcomes, strict=True):
        query_results, query_jobs, query_failures, answer, query_credits = outcome
        results.extend(query_results)
        jobs.extend(query_jobs)
        failures.extend(
            DiscoveryProviderFailure(
                provider=failure.provider,
                detail=f"{query}: {failure.detail}",
            )
            for failure in query_failures
        )
        if answer:
            answers.append(f"{query}: {answer}")
        if query_credits is not None:
            credits_used += query_credits

    if failures and not results and not jobs:
        detail = "; ".join(failure.detail for failure in failures)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"All selected discovery searches failed. {detail}",
        )
    return (
        deduplicate_discovery_results(results)[:max_results],
        jobs,
        failures,
        "\n\n".join(answers) or None,
        credits_used or None,
    )


async def _load_selected_discovery_tools(
    db: Session,
    options: TavilyDiscoverySearchRequest | None,
) -> tuple[
    list[DiscoveryProvider],
    dict[DiscoveryProvider, str | OctoparseSearchConfig | None],
    list[DiscoveryProviderFailure],
]:
    selected_tools = (
        options.providers
        if options is not None and options.providers is not None
        else await run_in_threadpool(get_selected_discovery_tools, db)
    )
    provider_configs: dict[
        DiscoveryProvider,
        str | OctoparseSearchConfig | None,
    ] = {}
    failures: list[DiscoveryProviderFailure] = []
    if DiscoveryProvider.TAVILY in selected_tools:
        try:
            provider_configs[DiscoveryProvider.TAVILY] = await run_in_threadpool(
                get_tavily_api_key,
                db,
            )
        except HTTPException as exc:
            failures.append(
                DiscoveryProviderFailure(
                    provider=DiscoveryProvider.TAVILY,
                    detail=str(exc.detail),
                )
            )
    if DiscoveryProvider.OCTOPARSE in selected_tools:
        try:
            provider_configs[DiscoveryProvider.OCTOPARSE] = await run_in_threadpool(
                get_octoparse_search_config,
                db,
            )
        except HTTPException as exc:
            failures.append(
                DiscoveryProviderFailure(
                    provider=DiscoveryProvider.OCTOPARSE,
                    detail=str(exc.detail),
                )
            )
    return selected_tools, provider_configs, failures


async def _search_with_preloaded_tools(
    query: str,
    max_results: int,
    db: Session,
    options: TavilyDiscoverySearchRequest | None,
    selected_tools: list[DiscoveryProvider],
    provider_configs: dict[
        DiscoveryProvider,
        str | OctoparseSearchConfig | None,
    ],
    initial_failures: list[DiscoveryProviderFailure],
) -> tuple[
    list[DiscoveryResult],
    list[StartedProviderJob],
    list[DiscoveryProviderFailure],
    str | None,
    float | None,
]:
    failures = list(initial_failures)

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
                options=options if provider == DiscoveryProvider.TAVILY else None,
                provider_config=provider_configs[provider],
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

    providers_to_run = [
        provider for provider in selected_tools if provider in provider_configs
    ]
    outcomes = await asyncio.gather(
        *(run_tool(provider) for provider in providers_to_run)
    )
    results: list[DiscoveryResult] = []
    jobs: list[StartedProviderJob] = []
    answer: str | None = None
    credits_used: float | None = None
    for _, outcome, failure in outcomes:
        if failure is not None:
            failures.append(failure)
        elif outcome is not None:
            results.extend(outcome.results)
            answer = outcome.answer or answer
            if outcome.credits_used is not None:
                credits_used = (credits_used or 0) + outcome.credits_used
            if outcome.job is not None:
                jobs.append(outcome.job)

    return results, jobs, failures, answer, credits_used


def distribute_result_limit(max_results: int, query_count: int) -> list[int]:
    base, remainder = divmod(max_results, query_count)
    return [
        base + (1 if index < remainder else 0)
        for index in range(query_count)
    ]


def deduplicate_discovery_results(
    results: list[DiscoveryResult],
) -> list[DiscoveryResult]:
    unique_results: list[DiscoveryResult] = []
    seen_domains: set[str] = set()
    for result in results:
        hostname = urlsplit(str(result.url)).hostname
        if hostname is None:
            continue
        domain = hostname.casefold()
        if domain.startswith("www."):
            domain = domain[4:]
        if domain in seen_domains:
            continue
        seen_domains.add(domain)
        unique_results.append(result)
    return unique_results


async def get_tavily_usage(db: Session) -> TavilyUsageResponse:
    api_key = await run_in_threadpool(get_tavily_api_key, db)
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Tavily is not configured.",
        )

    try:
        async with async_http_client(timeout=20.0) as client:
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
