from typing import Annotated
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_admin_context
from app.models import Organization, User
from app.schemas.auth import (
    AuthSessionResponse,
    AuthUserResponse,
    CredentialsInput,
    PasswordChange,
    OrganizationRegistration,
    OrganizationUserCreate,
    OrganizationUsersResponse,
)
from app.services.auth import (
    authenticate,
    REFRESH_TOKEN_COOKIE,
    clear_auth_cookies,
    create_auth_tokens,
    create_bootstrap_admin,
    create_organization_user,
    hash_password,
    register_organization,
    revoke_refresh_token,
    revoke_user_refresh_tokens,
    rotate_refresh_token,
    set_auth_cookies,
    validate_auth_configuration,
    verify_password,
)
from app.tenant_context import TenantContext, get_tenant_context


router = APIRouter(prefix="/auth", tags=["Authentication"])
DatabaseSession = Annotated[Session, Depends(get_db)]


@router.get("/bootstrap-status")
def bootstrap_status(db: DatabaseSession) -> dict[str, bool]:
    return {"required": db.scalar(select(func.count()).select_from(User)) == 0}


@router.post("/bootstrap", response_model=AuthSessionResponse)
def bootstrap_admin(
    payload: OrganizationRegistration,
    response: Response,
    db: DatabaseSession,
) -> AuthSessionResponse:
    validate_auth_configuration()
    user = create_bootstrap_admin(payload, db)
    return _start_session(user, response, db)


@router.post("/register-organization", response_model=AuthSessionResponse)
def register_new_organization(
    payload: OrganizationRegistration,
    response: Response,
    db: DatabaseSession,
) -> AuthSessionResponse:
    validate_auth_configuration()
    user = register_organization(payload, db)
    return _start_session(user, response, db)


@router.post("/login", response_model=AuthSessionResponse)
def login(
    payload: CredentialsInput,
    response: Response,
    db: DatabaseSession,
) -> AuthSessionResponse:
    validate_auth_configuration()
    user = authenticate(payload.email, payload.password, db)
    return _start_session(user, response, db)


@router.post("/refresh", response_model=AuthSessionResponse)
def refresh_session(
    request: Request,
    response: Response,
    db: DatabaseSession,
) -> AuthSessionResponse:
    refresh_token = request.cookies.get(REFRESH_TOKEN_COOKIE)
    if refresh_token:
        user, access_token, rotated_refresh_token = rotate_refresh_token(
            refresh_token,
            db,
        )
    else:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Your session has expired. Please sign in again.",
            headers={"WWW-Authenticate": "Cookie"},
        )

    set_auth_cookies(response, access_token, rotated_refresh_token)
    return _session_response(user, db)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    request: Request,
    response: Response,
    db: DatabaseSession,
) -> None:
    context = get_tenant_context()
    if context is not None:
        user = db.get(User, context.user_id)
        if user is not None:
            user.auth_version += 1
            revoke_user_refresh_tokens(user.id, db)
            db.commit()
    else:
        refresh_token = request.cookies.get(REFRESH_TOKEN_COOKIE)
        if refresh_token:
            revoke_refresh_token(refresh_token, db)
    clear_auth_cookies(response)


@router.post("/password", response_model=AuthSessionResponse)
def change_password(
    payload: PasswordChange,
    response: Response,
    request: Request,
    db: DatabaseSession,
) -> AuthSessionResponse:
    context = _required_context(request)
    user = db.get(User, context.user_id)
    if user is None or not verify_password(
        payload.current_password,
        user.password_hash,
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Current password is incorrect.",
        )
    user.password_hash = hash_password(payload.new_password)
    user.auth_version += 1
    revoke_user_refresh_tokens(user.id, db)
    db.commit()
    db.refresh(user)
    return _start_session(user, response, db)


@router.get("/me", response_model=AuthSessionResponse)
def read_session(
    request: Request,
    db: DatabaseSession,
) -> AuthSessionResponse:
    context = _required_context(request)
    user = db.get(User, context.user_id)
    organization = db.get(Organization, context.organization_id)
    if user is None or organization is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Your session is no longer valid.",
        )
    return AuthSessionResponse(
        user=AuthUserResponse.model_validate(user),
        organization_id=organization.id,
        organization_name=organization.name,
    )


@router.get("/users", response_model=OrganizationUsersResponse)
def list_organization_users(
    db: DatabaseSession,
) -> OrganizationUsersResponse:
    context = require_admin_context()
    users = db.scalars(
        select(User)
        .where(User.organization_id == context.organization_id)
        .order_by(User.created_at, User.email)
    ).all()
    return OrganizationUsersResponse(
        users=[AuthUserResponse.model_validate(user) for user in users]
    )


@router.post(
    "/users",
    response_model=AuthUserResponse,
    status_code=status.HTTP_201_CREATED,
)
def add_organization_user(
    payload: OrganizationUserCreate,
    db: DatabaseSession,
) -> User:
    context = require_admin_context()
    return create_organization_user(
        organization_id=context.organization_id,
        email=payload.email,
        password=payload.password,
        role=payload.role,
        db=db,
    )


def _start_session(
    user: User,
    response: Response,
    db: Session,
) -> AuthSessionResponse:
    access_token, refresh_token = create_auth_tokens(user, db)
    set_auth_cookies(response, access_token, refresh_token)
    return _session_response(user, db)


def _session_response(user: User, db: Session) -> AuthSessionResponse:
    organization = db.get(Organization, user.organization_id)
    if organization is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="The account organization is missing.",
        )
    return AuthSessionResponse(
        user=AuthUserResponse.model_validate(user),
        organization_id=organization.id,
        organization_name=organization.name,
    )


def _required_context(request: Request) -> TenantContext:
    context = get_tenant_context()
    if context is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication is required.",
        )
    return context


