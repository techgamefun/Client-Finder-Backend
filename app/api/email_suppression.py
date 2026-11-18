from typing import Annotated
from uuid import UUID
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_admin_context
from app.models import Organization, SuppressedContact
from app.schemas.email_suppression import (
    SuppressionCreate,
    SuppressionListResponse,
    SuppressionResponse,
    UnsubscribeRequest,
    UnsubscribeResponse,
)
from app.services.email_suppression import (
    add_suppression,
    unsubscribe_email,
    verify_unsubscribe_token,
)
from app.tenant_context import TenantContext


router = APIRouter(tags=["Email suppression"])
DatabaseSession = Annotated[Session, Depends(get_db)]
AdminContext = Annotated[TenantContext, Depends(require_admin_context)]


@router.get(
    "/unsubscribe",
    response_model=UnsubscribeResponse,
    include_in_schema=False,
)
def verify_unsubscribe(
    token: Annotated[str, Query(min_length=1, max_length=16_384)],
) -> UnsubscribeResponse:
    claims = verify_unsubscribe_token(token)
    return UnsubscribeResponse(valid=True, requires_email=claims.email is None)


@router.post(
    "/unsubscribe",
    response_model=UnsubscribeResponse,
    include_in_schema=False,
)
def unsubscribe(
    payload: UnsubscribeRequest,
    db: DatabaseSession,
) -> UnsubscribeResponse:
    claims = verify_unsubscribe_token(payload.token)
    email = unsubscribe_email(claims, payload.email)
    add_suppression(
        db,
        organization_id=_source_organization(db, claims.organization_id),
        target_type="email",
        value=email,
        reason="unsubscribe",
    )
    db.commit()
    return UnsubscribeResponse(valid=True)


@router.post("/unsubscribe/one-click", include_in_schema=False)
async def one_click_unsubscribe(
    request: Request,
    token: Annotated[str, Query(min_length=1, max_length=16_384)],
    db: DatabaseSession,
) -> Response:
    form = parse_qs((await request.body()).decode("ascii", errors="ignore"))
    if form.get("List-Unsubscribe") != ["One-Click"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The one-click unsubscribe request is malformed.",
        )
    claims = verify_unsubscribe_token(token)
    email = unsubscribe_email(claims)
    add_suppression(
        db,
        organization_id=_source_organization(db, claims.organization_id),
        target_type="email",
        value=email,
        reason="unsubscribe",
    )
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/email-suppressions",
    response_model=SuppressionListResponse,
)
def list_suppressions(
    db: DatabaseSession,
    admin: AdminContext,
) -> SuppressionListResponse:
    items = db.scalars(
        select(SuppressedContact)
        .where(SuppressedContact.organization_id == admin.organization_id)
        .order_by(SuppressedContact.created_at.desc())
        .limit(1000)
    ).all()
    return SuppressionListResponse(items=items)


@router.post(
    "/email-suppressions",
    response_model=SuppressionResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_suppression(
    payload: SuppressionCreate,
    db: DatabaseSession,
    admin: AdminContext,
) -> SuppressionResponse:
    record = add_suppression(
        db,
        organization_id=admin.organization_id,
        target_type=payload.target_type,
        value=payload.value,
        reason=payload.reason,
        created_by_user_id=admin.user_id,
    )
    db.commit()
    db.refresh(record)
    return record


def _source_organization(db: Session, organization_id: UUID) -> UUID | None:
    return db.scalar(
        select(Organization.id).where(Organization.id == organization_id)
    )
