from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_authenticated_context
from app.schemas.overview import OverviewRange, OverviewResponse
from app.services.overview import build_overview
from app.tenant_context import TenantContext


router = APIRouter(prefix="/overview", tags=["Overview"])
DatabaseSession = Annotated[Session, Depends(get_db)]
CurrentContext = Annotated[
    TenantContext,
    Depends(require_authenticated_context),
]


@router.get("", response_model=OverviewResponse)
def read_overview(
    context: CurrentContext,
    db: DatabaseSession,
    range_name: Annotated[OverviewRange, Query(alias="range")] = "30d",
    tz: Annotated[str, Query(min_length=1, max_length=64)] = "Asia/Kolkata",
) -> OverviewResponse:
    return build_overview(db, context, range_name, tz)
