from datetime import datetime, timezone
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.api.deps import get_db
from app.models import DiscoveryProviderJob, SearchKeyword
from app.schemas.discovery import (
    DiscoveryDomainRatingRequest,
    DiscoveryDomainRatingResult,
    DiscoveryIngestRequest,
    DiscoveryIngestResponse,
    DiscoverySearchResponse,
    DiscoveryProviderJobResponse,
    TavilyDiscoverySearchRequest,
    DiscoveryWebsiteSaveRequest,
    DiscoveryWebsiteSaveResponse,
    SearchKeywordResponse,
)
from app.services.discovery import StartedProviderJob, search_with_selected_tools
from app.services.ahrefs import fetch_discovery_domain_ratings
from app.services.discovery_websites import save_discovered_websites
from app.services.lead_ingestion import ingest_search_results
from app.services.octoparse import poll_octoparse_job

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
    results, started_jobs, provider_failures, answer, credits_used = (
        await search_with_selected_tools(
            query=payload.query,
            max_results=payload.max_results,
            db=db,
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
