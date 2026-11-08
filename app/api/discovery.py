from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.schemas.discovery import (
    DiscoverySearchRequest,
    DiscoverySearchResponse,
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
) -> DiscoverySearchResponse:
    return await search_companies(
        query=payload.query,
        max_results=payload.max_results,
    )


@router.post("/ingest")
async def ingest_companies(
    payload: DiscoverySearchRequest,
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, int | str]:
    return await ingest_search_results(payload, db)
