from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_authenticated_context
from app.config import settings
from app.models import PushSubscription, UserPushPreferences
from app.schemas.push_notifications import (
    PushPreferences,
    PushPreferencesUpdate,
    PushSettingsResponse,
    PushSubscriptionCreate,
    PushSubscriptionDelete,
    PushSubscriptionDeleteResponse,
    PushSubscriptionResponse,
)
from app.tenant_context import TenantContext


router = APIRouter(prefix="/notifications", tags=["Push notifications"])
DatabaseSession = Annotated[Session, Depends(get_db)]
AuthenticatedContext = Annotated[
    TenantContext,
    Depends(require_authenticated_context),
]


@router.get("/push/settings", response_model=PushSettingsResponse)
def read_push_settings(
    context: AuthenticatedContext,
    db: DatabaseSession,
) -> PushSettingsResponse:
    user_id = _required_user_id(context)
    subscriptions = db.scalars(
        select(PushSubscription)
        .where(
            PushSubscription.organization_id == context.organization_id,
            PushSubscription.user_id == user_id,
        )
        .order_by(PushSubscription.created_at.desc())
    ).all()
    preferences = _read_preferences(db, context)
    configured = settings.web_push_configured
    return PushSettingsResponse(
        configured=configured,
        public_key=settings.vapid_public_key if configured else None,
        subscriptions=[
            PushSubscriptionResponse(
                id=subscription.id,
                endpoint=subscription.endpoint,
                created_at=subscription.created_at,
            )
            for subscription in subscriptions
        ],
        preferences=preferences,
    )


@router.post(
    "/push/subscriptions",
    response_model=PushSubscriptionResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_push_subscription(
    payload: PushSubscriptionCreate,
    context: AuthenticatedContext,
    db: DatabaseSession,
    response: Response,
) -> PushSubscriptionResponse:
    if not settings.web_push_configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Push notifications are not configured on this server.",
        )
    user_id = _required_user_id(context)

    subscription = db.scalar(
        select(PushSubscription).where(
            PushSubscription.organization_id == context.organization_id,
            PushSubscription.user_id == user_id,
            PushSubscription.endpoint == payload.endpoint,
        )
    )
    created = subscription is None
    if subscription is None:
        subscription = PushSubscription(
            organization_id=context.organization_id,
            user_id=user_id,
            endpoint=payload.endpoint,
            p256dh=payload.keys.p256dh,
            auth=payload.keys.auth,
        )
        db.add(subscription)
    else:
        subscription.p256dh = payload.keys.p256dh
        subscription.auth = payload.keys.auth

    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        subscription = db.scalar(
            select(PushSubscription).where(
                PushSubscription.organization_id == context.organization_id,
                PushSubscription.user_id == user_id,
                PushSubscription.endpoint == payload.endpoint,
            )
        )
        if subscription is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "This browser subscription is registered to another account. "
                    "Remove it from that account before enabling notifications here."
                ),
            ) from exc
        subscription.p256dh = payload.keys.p256dh
        subscription.auth = payload.keys.auth
        db.commit()
        response.status_code = status.HTTP_200_OK
    else:
        if not created:
            response.status_code = status.HTTP_200_OK
    db.refresh(subscription)
    return PushSubscriptionResponse(
        id=subscription.id,
        endpoint=subscription.endpoint,
        created_at=subscription.created_at,
    )


@router.delete(
    "/push/subscriptions",
    response_model=PushSubscriptionDeleteResponse,
)
def delete_push_subscription(
    payload: PushSubscriptionDelete,
    context: AuthenticatedContext,
    db: DatabaseSession,
) -> PushSubscriptionDeleteResponse:
    user_id = _required_user_id(context)
    subscription = db.scalar(
        select(PushSubscription).where(
            PushSubscription.organization_id == context.organization_id,
            PushSubscription.user_id == user_id,
            PushSubscription.endpoint == payload.endpoint,
        )
    )
    if subscription is None:
        return PushSubscriptionDeleteResponse(removed=False)
    db.delete(subscription)
    db.commit()
    return PushSubscriptionDeleteResponse(removed=True)


@router.patch("/push/preferences", response_model=PushPreferences)
def update_push_preferences(
    payload: PushPreferencesUpdate,
    context: AuthenticatedContext,
    db: DatabaseSession,
) -> PushPreferences:
    user_id = _required_user_id(context)
    preferences = db.scalar(
        select(UserPushPreferences).where(
            UserPushPreferences.organization_id == context.organization_id,
            UserPushPreferences.user_id == user_id,
        )
    )
    if preferences is None:
        preferences = UserPushPreferences(
            organization_id=context.organization_id,
            user_id=user_id,
        )
        db.add(preferences)

    preferences.website_crawl_enabled = payload.website_crawl_enabled
    preferences.pagespeed_enabled = payload.pagespeed_enabled
    preferences.outreach_enabled = payload.outreach_enabled
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Notification preferences changed concurrently. Please retry.",
        ) from exc
    return PushPreferences(
        website_crawl_enabled=preferences.website_crawl_enabled,
        pagespeed_enabled=preferences.pagespeed_enabled,
        outreach_enabled=preferences.outreach_enabled,
    )


def _read_preferences(
    db: Session,
    context: TenantContext,
) -> PushPreferences:
    preferences = db.scalar(
        select(UserPushPreferences).where(
            UserPushPreferences.organization_id == context.organization_id,
            UserPushPreferences.user_id == _required_user_id(context),
        )
    )
    if preferences is None:
        return PushPreferences()
    return PushPreferences(
        website_crawl_enabled=preferences.website_crawl_enabled,
        pagespeed_enabled=preferences.pagespeed_enabled,
        outreach_enabled=preferences.outreach_enabled,
    )


def _required_user_id(context: TenantContext) -> UUID:
    if context.user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication is required.",
        )
    return context.user_id
