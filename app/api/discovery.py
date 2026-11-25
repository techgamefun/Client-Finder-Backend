import asyncio
from datetime import datetime, timezone
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.api.deps import get_db, require_authenticated_context
from app.models import DiscoveryProviderJob, SearchKeyword
from app.schemas.discovery import (
    DiscoveryAiSearchRequest,
    DiscoveryAiSearchResponse,
    DiscoveryDomainRatingRequest,
    DiscoveryDomainRatingResult,
    DiscoveryIngestRequest,
    DiscoveryIngestResponse,
    DiscoveryProvider,
    DiscoveryProviderFailure,
    DiscoveryProviderJobResponse,
    DiscoverySearchResponse,
    TavilyDiscoverySearchRequest,
    DiscoveryWebsiteSaveRequest,
    DiscoveryWebsiteSaveResponse,
    SearchKeywordResponse,
    parse_discovery_keywords,
)
from app.services.application_settings import (
    get_tavily_api_key,
    get_user_provider_api_key,
)
from app.services.discovery import (
    StartedProviderJob,
    deduplicate_discovery_results,
    distribute_result_limit,
    search_companies,
    search_multiple_with_selected_tools,
    search_with_selected_tools,
)
from app.services.discovery_query_generator import (
    generate_discovery_search_keywords,
)
from app.services.ahrefs import fetch_discovery_domain_ratings
from app.services.discovery_websites import save_discovered_websites
from app.services.lead_ingestion import ingest_search_results
from app.services.octoparse import poll_octoparse_job
from app.tenant_context import TenantContext

router = APIRouter(
    prefix="/discovery",
    tags=["Lead Discovery"],
)


def _persist_discovery_search(
    payload: TavilyDiscoverySearchRequest,
    started_jobs: list[StartedProviderJob],
    db: Session,
) -> tuple[UUID, list[DiscoveryProviderJobResponse]]:
    search = SearchKeyword(
        keyword=payload.query,
        searched_at=datetime.now(timezone.utc),
    )
    db.add(search)
    try:
        db.flush()
        provider_jobs = [
            DiscoveryProviderJob(
                search_keyword_id=search.id,
                provider=job.provider.value,
                external_task_id=job.external_task_id,
                lot_no=job.lot_no,
                max_results=job.max_results,
                status="running",
                result_mapping=job.result_mapping,
            )
            for job in started_jobs
        ]
        db.add_all(provider_jobs)
        db.commit()
        return search.id, [
            DiscoveryProviderJobResponse(
                id=job.id,
                provider=job.provider,
                status=job.status,
            )
            for job in provider_jobs
        ]
    except SQLAlchemyError:
        db.rollback()
        raise


@router.post(
    "/search",
    response_model=DiscoverySearchResponse,
)
async def discover_companies(
    payload: TavilyDiscoverySearchRequest,
    db: Annotated[Session, Depends(get_db)],
) -> DiscoverySearchResponse:
    queries = parse_discovery_keywords(payload.query)
    if len(queries) == 1:
        results, started_jobs, provider_failures, answer, credits_used = (
            await search_with_selected_tools(
                queries[0],
                payload.max_results,
                db,
                options=payload,
            )
        )
    else:
        results, started_jobs, provider_failures, answer, credits_used = (
            await search_multiple_with_selected_tools(
                queries,
                payload.max_results,
                db,
                options=payload,
            )
        )
    search_id, provider_jobs = await run_in_threadpool(
        _persist_discovery_search,
        payload,
        started_jobs,
        db,
    )
    return DiscoverySearchResponse(
        query=payload.query,
        results=results,
        search_id=search_id,
        answer=answer,
        credits_used=credits_used,
        provider_jobs=provider_jobs,
        provider_failures=provider_failures,
        search_queries=queries,
    )


@router.post(
    "/ai-search",
    response_model=DiscoveryAiSearchResponse,
)
async def discover_companies_with_ai(
    payload: DiscoveryAiSearchRequest,
    db: Annotated[Session, Depends(get_db)],
    context: Annotated[
        TenantContext,
        Depends(require_authenticated_context),
    ],
) -> DiscoveryAiSearchResponse:
    if context.user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="An authenticated user is required.",
        )
    ai_api_key = get_user_provider_api_key(
        db,
        payload.ai_provider,
        user_id=context.user_id,
    )
    if not ai_api_key:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Add your {payload.ai_provider.capitalize()} API key in "
                "Settings before using AI Search."
            ),
        )
    search_api_key = await run_in_threadpool(get_tavily_api_key, db)
    if not search_api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Configure Tavily in Settings to run AI Search.",
        )

    if payload.ai_provider == "gemini":
        fallback_api_key = get_user_provider_api_key(
            db,
            "groq",
            user_id=context.user_id,
        )
        keyword_generation = await generate_discovery_search_keywords(
            payload.query,
            payload.ai_provider,
            ai_api_key,
            model=payload.model,
            fallback_api_key=fallback_api_key,
        )
    else:
        keyword_generation = await generate_discovery_search_keywords(
            payload.query,
            payload.ai_provider,
            ai_api_key,
            model=payload.model,
        )
    queries = keyword_generation.keywords[: payload.max_results]

    result_counts = distribute_result_limit(payload.max_results, len(queries))
    search_tasks = [
        search_companies(
            query,
            db,
            max_results,
            options=payload.model_copy(
                update={
                    "query": query,
                    "max_results": max_results,
                    "providers": ["tavily"],
                },
            ),
            api_key=search_api_key,
            api_key_is_loaded=True,
        )
        for query, max_results in zip(queries, result_counts, strict=True)
    ]
    outcomes = await asyncio.gather(*search_tasks, return_exceptions=True)
    results = []
    failures = []
    answers = []
    credits_used = 0.0
    for query, outcome in zip(queries, outcomes, strict=True):
        if isinstance(outcome, HTTPException):
            failures.append(
                DiscoveryProviderFailure(
                    provider=DiscoveryProvider.TAVILY,
                    detail=f"{query}: {outcome.detail}",
                )
            )
            continue
        if isinstance(outcome, BaseException):
            raise outcome
        results.extend(outcome.results)
        if outcome.answer:
            answers.append(f"{query}: {outcome.answer}")
        if outcome.credits_used is not None:
            credits_used += outcome.credits_used

    if failures and not results:
        detail = "; ".join(failure.detail for failure in failures)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"All AI Search queries failed. {detail}",
        )

    search_id, _ = await run_in_threadpool(
        _persist_discovery_search,
        payload,
        [],
        db,
    )
    return DiscoveryAiSearchResponse(
        query=payload.query,
        results=deduplicate_discovery_results(results)[: payload.max_results],
        search_id=search_id,
        answer="\n\n".join(answers) or None,
        credits_used=credits_used or None,
        provider_failures=failures,
        search_queries=queries,
        ai_provider_used=keyword_generation.provider_used,
        ai_fallback_reason=keyword_generation.fallback_reason,
    )
@router.post(
    "/domain-ratings",
    response_model=list[DiscoveryDomainRatingResult],
)
async def get_discovery_domain_ratings(
    payload: DiscoveryDomainRatingRequest,
    db: Annotated[Session, Depends(get_db)],
) -> list[DiscoveryDomainRatingResult]:
    return await fetch_discovery_domain_ratings(payload.domains, db)


@router.get(
    "/jobs/{job_id}",
    response_model=DiscoveryProviderJobResponse,
)
async def read_discovery_provider_job(
    job_id: UUID,
    db: Annotated[Session, Depends(get_db)],
) -> DiscoveryProviderJobResponse:
    job = await run_in_threadpool(db.get, DiscoveryProviderJob, job_id)
    if job is None:
        raise HTTPException(
            status_code=404,
            detail="Discovery provider job not found.",
        )
    return await poll_octoparse_job(job, db)


@router.get(
    "/suggestions",
    response_model=list[SearchKeywordResponse],
)
def suggest_search_keywords(
    db: Annotated[Session, Depends(get_db)],
    query: Annotated[str, Query(max_length=300)] = "",
) -> list[SearchKeyword]:
    ranked_searches = (
        select(
            SearchKeyword.id.label("id"),
            func.row_number()
            .over(
                partition_by=func.lower(SearchKeyword.keyword),
                order_by=(
                    SearchKeyword.searched_at.desc(),
                    SearchKeyword.id.desc(),
                ),
            )
            .label("search_rank"),
        )
        .where(
            func.lower(SearchKeyword.keyword).contains(
                query.casefold(),
                autoescape=True,
            )
        )
        .subquery()
    )
    statement = (
        select(SearchKeyword)
        .join(ranked_searches, SearchKeyword.id == ranked_searches.c.id)
        .where(ranked_searches.c.search_rank == 1)
        .order_by(SearchKeyword.searched_at.desc())
        .limit(10)
    )
    return list(db.scalars(statement).all())


@router.post("/ingest", response_model=DiscoveryIngestResponse)
async def ingest_companies(
    payload: DiscoveryIngestRequest,
    db: Annotated[Session, Depends(get_db)],
) -> DiscoveryIngestResponse:
    return await ingest_search_results(payload, db)


@router.post(
    "/save-websites",
    response_model=DiscoveryWebsiteSaveResponse,
)
async def save_websites_for_review(
    payload: DiscoveryWebsiteSaveRequest,
    db: Annotated[Session, Depends(get_db)],
) -> DiscoveryWebsiteSaveResponse:
    return await save_discovered_websites(payload, db)
