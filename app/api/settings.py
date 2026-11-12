from typing import Annotated

from fastapi import APIRouter, Depends, Header
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.schemas.settings import (
    ApplicationSettingsResponse,
    ApplicationSettingsUpdate,
    TavilyUsageResponse,
)
from app.services.application_settings import (
    authorize_settings_admin,
    get_application_settings,
    save_application_settings,
)
from app.services.discovery import get_tavily_usage


router = APIRouter(prefix="/settings", tags=["Settings"])


def require_settings_admin(
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    authorize_settings_admin(authorization)


@router.get(
    "",
    response_model=ApplicationSettingsResponse,
    dependencies=[Depends(require_settings_admin)],
)
def read_settings(
    db: Annotated[Session, Depends(get_db)],
) -> ApplicationSettingsResponse:
    return get_application_settings(db)


@router.put(
    "",
    response_model=ApplicationSettingsResponse,
    dependencies=[Depends(require_settings_admin)],
)
def update_settings(
    payload: ApplicationSettingsUpdate,
    db: Annotated[Session, Depends(get_db)],
) -> ApplicationSettingsResponse:
    return save_application_settings(payload, db)


@router.get(
    "/tavily/usage",
    response_model=TavilyUsageResponse,
    dependencies=[Depends(require_settings_admin)],
)
async def read_tavily_usage(
    db: Annotated[Session, Depends(get_db)],
) -> TavilyUsageResponse:
    return await get_tavily_usage(db)
