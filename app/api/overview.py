from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import (
    get_db,
    require_admin_context,
    require_authenticated_context,
)
from app.schemas.overview import (
    AdminOverviewResponse,
    MemberActivityResponse,
    OverviewRange,
    OverviewResponse,
)
from app.services.overview import (
    build_admin_overview,
    build_member_activity,
    build_overview,
)
from app.tenant_context import TenantContext


router = APIRouter(prefix="/overview", tags=["Overview"])
DatabaseSession = Annotated[Session, Depends(get_db)]
CurrentContext = Annotated[
    TenantContext,
    Depends(require_authenticated_context),
]
AdminContext = Annotated[
    TenantContext,
    Depends(require_admin_context),
]


@router.get("", response_model=OverviewResponse)
def read_overview(
    context: CurrentContext,
    db: DatabaseSession,
    range_name: Annotated[OverviewRange, Query(alias="range")] = "30d",
    tz: Annotated[str, Query(min_length=1, max_length=64)] = "Asia/Kolkata",
) -> OverviewResponse:
    return build_overview(db, context, range_name, tz)


@router.get("/admin", response_model=AdminOverviewResponse)
def read_admin_overview(
    context: AdminContext,
    db: DatabaseSession,
    range_name: Annotated[OverviewRange, Query(alias="range")] = "30d",
    tz: Annotated[str, Query(min_length=1, max_length=64)] = "Asia/Kolkata",
) -> AdminOverviewResponse:
    return build_admin_overview(db, context, range_name, tz)


@router.get(
    "/admin/users/{user_id}/activity",
    response_model=MemberActivityResponse,
)
def read_admin_member_activity(
    user_id: UUID,
    context: AdminContext,
    db: DatabaseSession,
    range_name: Annotated[OverviewRange, Query(alias="range")] = "30d",
    tz: Annotated[str, Query(min_length=1, max_length=64)] = "Asia/Kolkata",
) -> MemberActivityResponse:
    return build_member_activity(db, context, user_id, range_name, tz)
