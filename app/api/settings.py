from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.schemas.settings import (
    ApplicationSettingsResponse,
    ApplicationSettingsUpdate,
    AhrefsUsageResponse,
    GroqModelsResponse,
    HypeStatUsageResponse,
    OctoparseTemplateSearchResponse,
    OctoparseUsageResponse,
    TavilyUsageResponse,
)
from app.services.application_settings import (
    get_application_settings,
    get_user_provider_api_key,
    save_application_settings,
)
from app.services.ahrefs import get_ahrefs_usage
from app.services.discovery import get_tavily_usage
from app.services.hypestat_usage import get_hypestat_usage
from app.services.octoparse import (
    get_octoparse_usage,
    search_octoparse_templates,
)
from app.services.groq_template_generator import list_available_draft_models
from app.tenant_context import get_tenant_context


router = APIRouter(prefix="/settings", tags=["Settings"])


@router.get(
    "/groq/models",
    response_model=GroqModelsResponse,
)
async def read_groq_models(
    db: Annotated[Session, Depends(get_db)],
) -> GroqModelsResponse:
    context = get_tenant_context()
    if context is None or context.user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="An authenticated user is required.",
        )
    api_key = get_user_provider_api_key(
        db,
        "groq",
        user_id=context.user_id,
    )
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Add your Groq API key in Settings before loading models.",
        )
    return GroqModelsResponse(
            models=await list_available_draft_models(api_key),
    )


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
    context = get_tenant_context()
    organization_fields = {
        "crawl_max_pages",
        "crawl_max_depth",
        "hypestat_auto_fetch_enabled",
        "selected_discovery_tools",
        "octoparse_template",
        "tavily_default_search_depth",
        "tavily_default_country",
        "tavily_default_exclude_domains",
    }
    if (
        context is not None
        and context.role != "admin"
        and organization_fields.intersection(payload.model_fields_set)
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Organization administrator privileges are required to change these settings.",
        )
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
    "/hypestat/usage",
    response_model=HypeStatUsageResponse,
)
async def read_hypestat_usage(
    db: Annotated[Session, Depends(get_db)],
) -> HypeStatUsageResponse:
    return await get_hypestat_usage(db)


@router.get(
    "/ahrefs/usage",
    response_model=AhrefsUsageResponse | None,
)
async def read_ahrefs_usage(
    db: Annotated[Session, Depends(get_db)],
) -> AhrefsUsageResponse | None:
    return await get_ahrefs_usage(db)


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
