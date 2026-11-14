from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.schemas.settings import (
    ApplicationSettingsResponse,
    ApplicationSettingsUpdate,
    OctoparseTemplateSearchResponse,
    OctoparseUsageResponse,
    TavilyUsageResponse,
)
from app.services.application_settings import (
    get_application_settings,
    save_application_settings,
)
from app.services.discovery import get_tavily_usage
from app.services.octoparse import (
    get_octoparse_usage,
    search_octoparse_templates,
)


router = APIRouter(prefix="/settings", tags=["Settings"])


@router.get(
    "",
    response_model=ApplicationSettingsResponse,
)
def read_settings(
    db: Annotated[Session, Depends(get_db)],
) -> ApplicationSettingsResponse:
    return get_application_settings(db)


@router.put(
    "",
    response_model=ApplicationSettingsResponse,
)
def update_settings(
    payload: ApplicationSettingsUpdate,
    db: Annotated[Session, Depends(get_db)],
) -> ApplicationSettingsResponse:
    return save_application_settings(payload, db)


@router.get(
    "/tavily/usage",
    response_model=TavilyUsageResponse,
)
async def read_tavily_usage(
    db: Annotated[Session, Depends(get_db)],
) -> TavilyUsageResponse:
    return await get_tavily_usage(db)


@router.get(
    "/octoparse/usage",
    response_model=OctoparseUsageResponse,
)
def read_octoparse_usage(
    db: Annotated[Session, Depends(get_db)],
) -> OctoparseUsageResponse:
    return get_octoparse_usage(db)


@router.get(
    "/octoparse/templates",
    response_model=OctoparseTemplateSearchResponse,
)
async def read_octoparse_templates(
    db: Annotated[Session, Depends(get_db)],
    keyword: Annotated[str, Query(min_length=2, max_length=200)],
) -> OctoparseTemplateSearchResponse:
    return await search_octoparse_templates(keyword, db)
