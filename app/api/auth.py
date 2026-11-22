import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_admin_context
from app.config import settings
from app.models import (
    Organization,
    OrganizationInvitation,
    OrganizationMembership,
    User,
)
from app.schemas.auth import (
    AccountProfileUpdate,
    AuthSessionResponse,
    AuthUserResponse,
    CredentialsInput,
    OrganizationInvitationAccept,
    OrganizationInvitationCreate,
    OrganizationInvitationListItem,
    OrganizationInvitationPreview,
    OrganizationInvitationResponse,
    OrganizationOption,
    OrganizationProfileResponse,
    OrganizationProfileUpdate,
    PasswordChange,
    OrganizationRegistration,
    OrganizationSelection,
    OrganizationUserRoleUpdate,
    OrganizationUsersResponse,
)
from app.services.auth import (
    authenticate,
    REFRESH_TOKEN_COOKIE,
    clear_auth_cookies,
    create_auth_tokens,
    create_bootstrap_admin,
    create_organization_membership,
    hash_password,
    register_organization,
    revoke_refresh_token,
    revoke_user_refresh_tokens,
    rotate_refresh_token,
    set_auth_cookies,
    validate_auth_configuration,
    verify_password,
)
from app.tenant_context import (
    TenantContext,
    get_tenant_context,
    reset_tenant_context,
    set_tenant_context,
)


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
        (
            user,
            organization_id,
            access_token,
            rotated_refresh_token,
        ) = rotate_refresh_token(refresh_token, db)
    else:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Your session has expired. Please sign in again.",
            headers={"WWW-Authenticate": "Cookie"},
        )

    set_auth_cookies(response, access_token, rotated_refresh_token)
    return _session_response(user, db, organization_id=organization_id)


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
    return _start_session(
        user,
        response,
        db,
        organization_id=context.organization_id,
    )


@router.get("/me", response_model=AuthSessionResponse)
def read_session(
    request: Request,
    db: DatabaseSession,
) -> AuthSessionResponse:
    context = _required_context(request)
    user = db.get(User, context.user_id)
    organization = db.get(Organization, context.organization_id)
    membership = db.scalar(
        select(OrganizationMembership).where(
            OrganizationMembership.user_id == context.user_id,
            OrganizationMembership.organization_id == context.organization_id,
            OrganizationMembership.is_active.is_(True),
        )
    )
    if user is None or organization is None or membership is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Your session is no longer valid.",
        )
    return _session_response(
        user,
        db,
        organization_id=organization.id,
    )


@router.get("/users", response_model=OrganizationUsersResponse)
def list_organization_users(
    db: DatabaseSession,
) -> OrganizationUsersResponse:
    context = require_admin_context()
    rows = db.execute(
        select(User, OrganizationMembership)
        .join(OrganizationMembership, OrganizationMembership.user_id == User.id)
        .where(
            OrganizationMembership.organization_id == context.organization_id,
        )
        .order_by(OrganizationMembership.joined_at, User.email)
    ).all()
    return OrganizationUsersResponse(
        users=[
            _organization_user_response(user, membership)
            for user, membership in rows
        ]
    )


@router.get(
    "/invitations",
    response_model=list[OrganizationInvitationListItem],
)
def list_organization_invitations(
    db: DatabaseSession,
) -> list[OrganizationInvitationListItem]:
    context = require_admin_context()
    now = datetime.now(timezone.utc)
    invitations = db.scalars(
        select(OrganizationInvitation)
        .where(
            OrganizationInvitation.organization_id == context.organization_id,
            OrganizationInvitation.accepted_at.is_(None),
            OrganizationInvitation.revoked_at.is_(None),
            OrganizationInvitation.expires_at > now,
        )
        .order_by(OrganizationInvitation.created_at.desc())
    ).all()
    return [
        OrganizationInvitationListItem.model_validate(invitation)
        for invitation in invitations
    ]


@router.post(
    "/invitations",
    response_model=OrganizationInvitationResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_organization_invitation(
    payload: OrganizationInvitationCreate,
    db: DatabaseSession,
) -> OrganizationInvitationResponse:
    context = require_admin_context()
    membership = db.scalar(
        select(OrganizationMembership).join(
            User,
            User.id == OrganizationMembership.user_id,
        ).where(
            OrganizationMembership.organization_id == context.organization_id,
            User.email == payload.email,
            OrganizationMembership.is_active.is_(True),
            User.is_active.is_(True),
        )
    )
    if membership is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This person is already an active member of the organization.",
        )

    now = datetime.now(timezone.utc)
    for previous in db.scalars(
        select(OrganizationInvitation)
        .where(
            OrganizationInvitation.organization_id == context.organization_id,
            OrganizationInvitation.email == payload.email,
            OrganizationInvitation.accepted_at.is_(None),
            OrganizationInvitation.revoked_at.is_(None),
        )
        .with_for_update()
    ).all():
        previous.revoked_at = now

    token = secrets.token_urlsafe(32)
    invitation = OrganizationInvitation(
        organization_id=context.organization_id,
        email=payload.email,
        role=payload.role,
        token_hash=hashlib.sha256(token.encode("ascii")).hexdigest(),
        invited_by_user_id=context.user_id,
        expires_at=now + timedelta(days=7),
    )
    db.add(invitation)
    db.commit()
    db.refresh(invitation)
    frontend_origin = settings.frontend_origins.split(",", maxsplit=1)[0].strip()
    return OrganizationInvitationResponse(
        invitation_url=f"{frontend_origin}/invitations/accept?token={token}",
        email=invitation.email,
        role=invitation.role,
        expires_at=invitation.expires_at,
    )


@router.get(
    "/invitations/{token}",
    response_model=OrganizationInvitationPreview,
)
def read_organization_invitation(
    token: str,
    db: DatabaseSession,
) -> OrganizationInvitationPreview:
    invitation = _get_pending_invitation(token, db)
    organization = db.get(Organization, invitation.organization_id)
    if organization is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="This invitation is no longer available.",
        )
    return OrganizationInvitationPreview(
        organization_name=organization.name,
        email=invitation.email,
        role=invitation.role,
        expires_at=invitation.expires_at,
    )


@router.post(
    "/invitations/{token}/accept",
    response_model=AuthSessionResponse,
)
def accept_organization_invitation(
    token: str,
    payload: OrganizationInvitationAccept,
    response: Response,
    db: DatabaseSession,
) -> AuthSessionResponse:
    invitation = _get_pending_invitation(token, db, lock=True)
    session_context = db.info.pop("tenant_context", None)
    tenant_context_token = set_tenant_context(None)
    try:
        existing_user = db.scalar(
            select(User).where(User.email == invitation.email)
        )
        if existing_user is None:
            if not payload.first_name or not payload.last_name:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail=(
                        "Could not verify account access. Check your password, "
                        "or choose account creation if you are new to the platform."
                    ),
                )
            user = User(
                organization_id=invitation.organization_id,
                email=invitation.email,
                password_hash=hash_password(payload.password),
                role=invitation.role,
                first_name=payload.first_name,
                last_name=payload.last_name,
            )
            db.add(user)
            try:
                db.flush()
            except IntegrityError as exc:
                db.rollback()
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="The invitation could not be accepted.",
                ) from exc
        else:
            try:
                user = authenticate(
                    invitation.email,
                    payload.password,
                    db,
                )
            except HTTPException as exc:
                if exc.status_code != status.HTTP_401_UNAUTHORIZED:
                    raise
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail=(
                        "Could not verify account access. Check your password, "
                        "or choose account creation if you are new to the platform."
                    ),
                ) from exc
    finally:
        reset_tenant_context(tenant_context_token)
        if session_context is not None:
            db.info["tenant_context"] = session_context

    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="This account is not available.",
        )

    membership = db.scalar(
        select(OrganizationMembership).where(
            OrganizationMembership.user_id == user.id,
            OrganizationMembership.organization_id == invitation.organization_id,
        )
    )
    if membership is not None and membership.is_active:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This account is already a member of the organization.",
        )
    if membership is None:
        membership = create_organization_membership(
            organization_id=invitation.organization_id,
            user_id=user.id,
            role=invitation.role,
            db=db,
        )
    else:
        membership.role = invitation.role
        membership.is_active = True
        membership.joined_at = datetime.now(timezone.utc)
    if user.organization_id == invitation.organization_id:
        user.role = invitation.role

    invitation.accepted_at = datetime.now(timezone.utc)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The invitation could not be accepted. It may already have been used.",
        ) from exc

    access_token, refresh_token = create_auth_tokens(
        user,
        db,
        organization_id=invitation.organization_id,
    )
    set_auth_cookies(response, access_token, refresh_token)
    return _session_response(
        user,
        db,
        organization_id=invitation.organization_id,
    )


@router.delete(
    "/invitations/{invitation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def revoke_organization_invitation(
    invitation_id: UUID,
    db: DatabaseSession,
) -> None:
    context = require_admin_context()
    invitation = db.scalar(
        select(OrganizationInvitation).where(
            OrganizationInvitation.id == invitation_id,
            OrganizationInvitation.organization_id == context.organization_id,
            OrganizationInvitation.accepted_at.is_(None),
            OrganizationInvitation.revoked_at.is_(None),
        )
    )
    if invitation is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The pending invitation was not found.",
        )
    invitation.revoked_at = datetime.now(timezone.utc)
    db.commit()


@router.post("/select-organization", response_model=AuthSessionResponse)
def select_organization(
    payload: OrganizationSelection,
    request: Request,
    response: Response,
    db: DatabaseSession,
) -> AuthSessionResponse:
    context = _required_context(request)
    if context.user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication is required.",
        )
    user = db.get(User, context.user_id)
    membership = db.scalar(
        select(OrganizationMembership).where(
            OrganizationMembership.user_id == context.user_id,
            OrganizationMembership.organization_id == payload.organization_id,
            OrganizationMembership.is_active.is_(True),
        )
    )
    organization = db.get(Organization, payload.organization_id)
    if user is None or membership is None or organization is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The organization membership was not found.",
        )
    refresh_token = request.cookies.get(REFRESH_TOKEN_COOKIE)
    if refresh_token:
        revoke_refresh_token(refresh_token, db)
    access_token, next_refresh_token = create_auth_tokens(
        user,
        db,
        organization_id=organization.id,
    )
    set_auth_cookies(response, access_token, next_refresh_token)
    return _session_response(user, db, organization_id=organization.id)


@router.patch("/users/{user_id}/role", response_model=AuthUserResponse)
def update_organization_user_role(
    user_id: UUID,
    payload: OrganizationUserRoleUpdate,
    db: DatabaseSession,
) -> AuthUserResponse:
    context = require_admin_context()
    organization = db.scalar(
        select(Organization)
        .where(Organization.id == context.organization_id)
        .with_for_update()
    )
    if organization is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Organization not found.",
        )
    membership_row = db.execute(
        select(User, OrganizationMembership)
        .join(OrganizationMembership, OrganizationMembership.user_id == User.id)
        .where(
            User.id == user_id,
            OrganizationMembership.organization_id == context.organization_id,
        )
        .with_for_update()
    ).one_or_none()
    if membership_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Organization member not found.",
        )
    user, membership = membership_row
    if user.id == context.user_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="You cannot change your own role.",
        )
    if (
        membership.role == "admin"
        and payload.role == "user"
        and membership.is_active
        and user.is_active
    ):
        active_admins = db.scalar(
            select(func.count(OrganizationMembership.id))
            .select_from(OrganizationMembership)
            .join(User, User.id == OrganizationMembership.user_id)
            .where(
                OrganizationMembership.organization_id == context.organization_id,
                OrganizationMembership.role == "admin",
                OrganizationMembership.is_active.is_(True),
                User.is_active.is_(True),
            )
        )
        if active_admins is not None and active_admins <= 1:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="The last active administrator cannot be demoted.",
            )
    membership.role = payload.role
    if user.organization_id == context.organization_id:
        user.role = payload.role
    db.commit()
    return _organization_user_response(user, membership)


@router.patch(
    "/organization",
    response_model=OrganizationProfileResponse,
)
def update_organization_profile(
    payload: OrganizationProfileUpdate,
    db: DatabaseSession,
) -> OrganizationProfileResponse:
    context = require_admin_context()
    organization = db.get(Organization, context.organization_id)
    if organization is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Organization not found.",
        )
    organization.description = payload.description
    db.commit()
    return OrganizationProfileResponse(description=organization.description)


@router.patch("/me/profile", response_model=AuthSessionResponse)
def update_account_profile(
    payload: AccountProfileUpdate,
    request: Request,
    db: DatabaseSession,
) -> AuthSessionResponse:
    context = _required_context(request)
    user = db.get(User, context.user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Your account is no longer available.",
        )
    user.first_name = payload.first_name
    user.last_name = payload.last_name
    db.commit()
    db.refresh(user)
    return _session_response(
        user,
        db,
        organization_id=context.organization_id,
    )


def _start_session(
    user: User,
    response: Response,
    db: Session,
    *,
    organization_id: UUID | None = None,
) -> AuthSessionResponse:
    active_organization_id = organization_id or user.organization_id
    access_token, refresh_token = create_auth_tokens(
        user,
        db,
        organization_id=active_organization_id,
    )
    set_auth_cookies(response, access_token, refresh_token)
    return _session_response(
        user,
        db,
        organization_id=active_organization_id,
    )


def _session_response(
    user: User,
    db: Session,
    *,
    organization_id: UUID | None = None,
) -> AuthSessionResponse:
    active_organization_id = organization_id or user.organization_id
    organization = db.get(Organization, active_organization_id)
    memberships = db.execute(
        select(OrganizationMembership, Organization)
        .join(Organization, Organization.id == OrganizationMembership.organization_id)
        .where(
            OrganizationMembership.user_id == user.id,
            OrganizationMembership.is_active.is_(True),
        )
        .order_by(Organization.name)
    ).all()
    active_membership = next(
        (
            membership
            for membership, _ in memberships
            if membership.organization_id == active_organization_id
        ),
        None,
    )
    if organization is None or active_membership is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="The active organization membership is missing.",
        )
    user_response = AuthUserResponse.model_validate(user).model_copy(
        update={"role": active_membership.role},
    )
    return AuthSessionResponse(
        user=user_response,
        organizations=[
            OrganizationOption(
                id=membership.organization_id,
                name=member_organization.name,
                role=membership.role,
            )
            for membership, member_organization in memberships
        ],
        organization_id=organization.id,
        organization_name=organization.name,
        organization_description=organization.description,
        organization_logo_url=organization.logo_url,
        organization_banner_url=organization.banner_url,
    )


def _organization_user_response(
    user: User,
    membership: OrganizationMembership,
) -> AuthUserResponse:
    response = AuthUserResponse.model_validate(user)
    return response.model_copy(
        update={
            "role": membership.role,
            "is_active": user.is_active and membership.is_active,
        },
    )


def _get_pending_invitation(
    token: str,
    db: Session,
    *,
    lock: bool = False,
) -> OrganizationInvitation:
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    statement = select(OrganizationInvitation).where(
        OrganizationInvitation.token_hash == token_hash,
        OrganizationInvitation.accepted_at.is_(None),
        OrganizationInvitation.revoked_at.is_(None),
    )
    if lock:
        statement = statement.with_for_update()
    invitation = db.scalar(statement)
    if invitation is None or _as_utc(invitation.expires_at) <= datetime.now(
        timezone.utc,
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="This invitation is no longer available.",
        )
    return invitation


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _required_context(request: Request) -> TenantContext:
    context = get_tenant_context()
    if context is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication is required.",
        )
    return context


