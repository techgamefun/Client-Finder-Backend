
from fastapi import APIRouter

from app.schemas.discovery import (
    DiscoverySearchRequest,
    DiscoverySearchResponse,
)
from app.services.discovery import search_companies


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
