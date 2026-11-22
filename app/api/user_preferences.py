from copy import deepcopy
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_authenticated_context
from app.models import UserPreferences
from app.schemas.user_preferences import (
    UserPreferencesResponse,
    UserPreferencesUpdate,
)
from app.services.user_preferences_cache import (
    invalidate_cached_preferences,
    read_cached_preferences,
    write_cached_preferences,
)
from app.tenant_context import TenantContext


router = APIRouter(prefix="/preferences", tags=["User preferences"])
DatabaseSession = Annotated[Session, Depends(get_db)]
AuthenticatedContext = Annotated[
    TenantContext,
    Depends(require_authenticated_context),
]


@router.get("", response_model=UserPreferencesResponse)
def read_user_preferences(
    context: AuthenticatedContext,
    db: DatabaseSession,
) -> UserPreferencesResponse:
    user_id = _required_user_id(context)
    cached = read_cached_preferences(context.organization_id, user_id)
    if cached is not None:
        try:
            return UserPreferencesResponse.model_validate(cached)
        except ValueError:
            invalidate_cached_preferences(context.organization_id, user_id)

    row = _get_preferences_row(db, context.organization_id, user_id)
    if row is None:
        preferences = UserPreferencesResponse()
    else:
        preferences = UserPreferencesResponse.model_validate(row.preferences)
    write_cached_preferences(
        context.organization_id,
        user_id,
        preferences.model_dump(mode="json"),
    )
    return preferences


@router.patch("", response_model=UserPreferencesResponse)
def update_user_preferences(
    payload: UserPreferencesUpdate,
    context: AuthenticatedContext,
    db: DatabaseSession,
) -> UserPreferencesResponse:
    user_id = _required_user_id(context)
    row = _get_preferences_row(db, context.organization_id, user_id)
    if row is None:
        row = UserPreferences(
            user_id=user_id,
            organization_id=context.organization_id,
            preferences={},
        )
        db.add(row)

    preferences = deepcopy(row.preferences or {})
    _merge_preferences(
        preferences,
        payload.model_dump(mode="json", exclude_unset=True),
    )
    validated = UserPreferencesResponse.model_validate(preferences)
    row.preferences = validated.model_dump(mode="json", exclude_unset=True)
    try:
        db.commit()
        db.refresh(row)
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="User preferences changed concurrently. Please retry.",
        ) from exc
    preferences = UserPreferencesResponse.model_validate(row.preferences)
    invalidate_cached_preferences(context.organization_id, user_id)
    return preferences


def _get_preferences_row(
    db: Session,
    organization_id: UUID,
    user_id: UUID,
) -> UserPreferences | None:
    return db.scalar(
        select(UserPreferences).where(
            UserPreferences.organization_id == organization_id,
            UserPreferences.user_id == user_id,
        )
    )


def _merge_preferences(
    current: dict[str, Any],
    patch: dict[str, Any],
) -> None:
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(current.get(key), dict):
            _merge_preferences(current[key], value)
        else:
            current[key] = value


def _required_user_id(context: TenantContext) -> UUID:
    if context.user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication is required.",
        )
    return context.user_id
