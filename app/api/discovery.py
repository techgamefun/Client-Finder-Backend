from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.models import SearchKeyword
from app.schemas.discovery import (
    DiscoveryIngestRequest,
    DiscoveryIngestResponse,
    DiscoverySearchRequest,
    DiscoverySearchResponse,
    SearchKeywordResponse,
)
from app.services.discovery import search_companies
from app.services.lead_ingestion import ingest_search_results

router = APIRouter(
    prefix="/discovery",
    tags=["Lead Discovery"],
)


@router.post(
    "/search",
    response_model=DiscoverySearchResponse,
)
async def discover_companies(
    payload: DiscoverySearchRequest,
    db: Annotated[Session, Depends(get_db)],
) -> DiscoverySearchResponse:
    response = await search_companies(
        query=payload.query,
        max_results=payload.max_results,
    )
    search = SearchKeyword(
        keyword=payload.query,
        searched_at=datetime.now(timezone.utc),
    )
    db.add(search)
    try:
        db.commit()
        db.refresh(search)
    except SQLAlchemyError:
        db.rollback()
        raise
    return response.model_copy(update={"search_id": search.id})


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
