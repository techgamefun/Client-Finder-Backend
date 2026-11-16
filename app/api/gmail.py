import hmac
import logging
from datetime import datetime, timezone
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import (
    get_db,
    require_admin_context,
    require_authenticated_context,
)
from app.config import settings
from app.models import (
    ConnectedGmailAccount,
    User,
    UserGmailAssignment,
)
from app.schemas.gmail import (
    AssignedGmailAccountResponse,
    ConnectedGmailAccountResponse,
    GmailComposeInput,
    GmailLabelResponse,
    GmailMessageDetailResponse,
    GmailMessagesResponse,
    GmailReplyInput,
    GmailReplyResponse,
    GmailAccountAssigneeResponse,
    UserGmailAssignmentCreate,
    UserGmailAssignmentResponse,
)
from app.services.gmail import (
    compose_gmail_message,
    encrypt_refresh_token,
    get_gmail_message,
    list_gmail_labels,
    list_gmail_messages,
    reply_to_gmail_message,
)
from app.services.gmail_oauth import (
    GoogleOAuthState,
    decode_google_oauth_state,
    exchange_google_authorization_code,
    fetch_google_profile,
)
from app.tenant_context import (
    TenantContext,
    get_tenant_context,
    reset_tenant_context,
    set_tenant_context,
)


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/gmail", tags=["Gmail"])
DatabaseSession = Annotated[Session, Depends(get_db)]
AdminContext = Annotated[TenantContext, Depends(require_admin_context)]
CurrentContext = Annotated[
    TenantContext,
    Depends(require_authenticated_context),
]
GMAIL_OAUTH_STATE_COOKIE = "gmail_oauth_nonce"
GMAIL_CALLBACK_PATH = "/api/v1/gmail/callback"


@router.get(
    "/callback",
    include_in_schema=False,
)
async def google_oauth_callback(
    request: Request,
    db: DatabaseSession,
    state: str,
    code: str | None = None,
    error: str | None = None,
) -> RedirectResponse:
    oauth_state = decode_google_oauth_state(state)
    cookie_nonce = request.cookies.get(GMAIL_OAUTH_STATE_COOKIE)
    if cookie_nonce is None or not hmac.compare_digest(
        cookie_nonce,
        oauth_state.nonce,
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The Google OAuth state cookie is missing or invalid.",
        )
    admin = _validate_admin_state(oauth_state, db)
    current_context = get_tenant_context()
    if current_context is not None and (
        current_context.organization_id != oauth_state.org_id
        or current_context.user_id != oauth_state.admin_user_id
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="The signed-in account does not match the Google OAuth request.",
        )

    if error is not None:
        return _frontend_redirect("error")
    if not code:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Google did not return an authorization code.",
        )

    tokens = await exchange_google_authorization_code(code)
    profile = await fetch_google_profile(tokens.access_token)
    tenant_token = set_tenant_context(
        TenantContext(
            organization_id=oauth_state.org_id,
            user_id=admin.id,
            email=admin.email,
            role=admin.role,
        )
    )
    try:
        account = db.scalar(
            select(ConnectedGmailAccount).where(
                ConnectedGmailAccount.google_account_id
                == profile.google_account_id
            )
        )
        if account is not None and account.org_id != oauth_state.org_id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This Google account is already connected to another organization.",
            )
        if account is None and not tokens.refresh_token:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Google did not return a refresh token. Reconnect and approve offline access.",
            )

        now = datetime.now(timezone.utc)
        if account is None:
            account = ConnectedGmailAccount(
                org_id=oauth_state.org_id,
                email_address=profile.email_address,
                google_account_id=profile.google_account_id,
                encrypted_refresh_token=encrypt_refresh_token(
                    tokens.refresh_token or ""
                ),
                token_updated_at=now,
            )
            db.add(account)
        else:
            account.email_address = profile.email_address
            if tokens.refresh_token:
                account.encrypted_refresh_token = encrypt_refresh_token(
                    tokens.refresh_token
                )
                account.token_updated_at = now
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        logger.info("Google account connection conflicted with an existing record.")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This Google account is already connected.",
        ) from exc
    finally:
        reset_tenant_context(tenant_token)

    return _frontend_redirect("connected")


@router.get("/accounts", response_model=list[ConnectedGmailAccountResponse])
def list_connected_gmail_accounts(
    context: AdminContext,
    db: DatabaseSession,
) -> list[ConnectedGmailAccountResponse]:
    accounts = list(
        db.scalars(
            select(ConnectedGmailAccount)
            .where(ConnectedGmailAccount.org_id == context.organization_id)
            .order_by(ConnectedGmailAccount.email_address)
        ).all()
    )
    if not accounts:
        return []

    account_ids = [account.id for account in accounts]
    assigned_users: dict[UUID, list[GmailAccountAssigneeResponse]] = {
        account_id: [] for account_id in account_ids
    }
    rows = db.execute(
        select(UserGmailAssignment, User)
        .join(User, User.id == UserGmailAssignment.user_id)
        .where(
            UserGmailAssignment.org_id == context.organization_id,
            UserGmailAssignment.gmail_account_id.in_(account_ids),
        )
        .order_by(User.email)
    ).all()
    for assignment, user in rows:
        assigned_users[assignment.gmail_account_id].append(
            GmailAccountAssigneeResponse(
                assignment_id=assignment.id,
                user_id=user.id,
                email=user.email,
                assigned_at=assignment.created_at,
            )
        )
    return [
        ConnectedGmailAccountResponse(
            id=account.id,
            email_address=account.email_address,
            google_account_id=account.google_account_id,
            token_updated_at=account.token_updated_at,
            assigned_users=assigned_users[account.id],
        )
        for account in accounts
    ]


@router.get(
    "/accounts/assigned",
    response_model=list[AssignedGmailAccountResponse],
)
def list_assigned_gmail_accounts(
    context: CurrentContext,
    db: DatabaseSession,
) -> list[ConnectedGmailAccount]:
    return list(
        db.scalars(
            select(ConnectedGmailAccount)
            .join(
                UserGmailAssignment,
                UserGmailAssignment.gmail_account_id
                == ConnectedGmailAccount.id,
            )
            .where(
                ConnectedGmailAccount.org_id == context.organization_id,
                UserGmailAssignment.org_id == context.organization_id,
                UserGmailAssignment.user_id == context.user_id,
            )
            .order_by(ConnectedGmailAccount.email_address)
        ).all()
    )


@router.get(
    "/accounts/{gmail_account_id}/labels",
    response_model=list[GmailLabelResponse],
)
def read_gmail_labels(
    gmail_account_id: UUID,
    _context: CurrentContext,
    db: DatabaseSession,
) -> list[GmailLabelResponse]:
    return list_gmail_labels(db, gmail_account_id)


@router.get(
    "/accounts/{gmail_account_id}/messages",
    response_model=GmailMessagesResponse,
)
def read_gmail_inbox(
    gmail_account_id: UUID,
    _context: CurrentContext,
    db: DatabaseSession,
    label_id: str = Query(default="INBOX", min_length=1, max_length=225),
    q: str | None = Query(default=None, max_length=200),
    page_token: str | None = Query(default=None, max_length=4096),
    max_results: int = Query(default=25, ge=1, le=50),
) -> GmailMessagesResponse:
    return list_gmail_messages(
        db,
        gmail_account_id,
        label_id=label_id,
        query=q,
        page_token=page_token,
        max_results=max_results,
    )


@router.post(
    "/accounts/{gmail_account_id}/messages",
    response_model=GmailReplyResponse,
    status_code=status.HTTP_201_CREATED,
)
def compose_message(
    gmail_account_id: UUID,
    payload: GmailComposeInput,
    _context: CurrentContext,
    db: DatabaseSession,
) -> GmailReplyResponse:
    return compose_gmail_message(db, gmail_account_id, payload)


@router.get(
    "/accounts/{gmail_account_id}/messages/{message_id}",
    response_model=GmailMessageDetailResponse,
)
def read_gmail_message(
    gmail_account_id: UUID,
    message_id: str,
    _context: CurrentContext,
    db: DatabaseSession,
) -> GmailMessageDetailResponse:
    return get_gmail_message(db, gmail_account_id, message_id)


@router.post(
    "/accounts/{gmail_account_id}/messages/{message_id}/reply",
    response_model=GmailReplyResponse,
)
def reply_to_message(
    gmail_account_id: UUID,
    message_id: str,
    payload: GmailReplyInput,
    _context: CurrentContext,
    db: DatabaseSession,
) -> GmailReplyResponse:
    return reply_to_gmail_message(
        db,
        gmail_account_id,
        message_id,
        payload.body_text,
    )


@router.post(
    "/accounts/{gmail_account_id}/assignments",
    response_model=UserGmailAssignmentResponse,
    status_code=status.HTTP_201_CREATED,
)
def assign_gmail_account(
    gmail_account_id: UUID,
    payload: UserGmailAssignmentCreate,
    context: AdminContext,
    db: DatabaseSession,
) -> UserGmailAssignment:
    account = db.scalar(
        select(ConnectedGmailAccount).where(
            ConnectedGmailAccount.id == gmail_account_id,
            ConnectedGmailAccount.org_id == context.organization_id,
        )
    )
    if account is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The connected Gmail account was not found.",
        )
    user = db.scalar(
        select(User).where(
            User.id == payload.user_id,
            User.organization_id == context.organization_id,
            User.is_active.is_(True),
        )
    )
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The active organization user was not found.",
        )

    assignment = UserGmailAssignment(
        org_id=context.organization_id,
        user_id=user.id,
        gmail_account_id=account.id,
        assigned_by=context.user_id,
    )
    db.add(assignment)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This Gmail account is already assigned to that user.",
        ) from exc
    db.refresh(assignment)
    return assignment


@router.delete(
    "/assignments/{assignment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def remove_gmail_assignment(
    assignment_id: UUID,
    context: AdminContext,
    db: DatabaseSession,
) -> None:
    assignment = db.scalar(
        select(UserGmailAssignment).where(
            UserGmailAssignment.id == assignment_id,
            UserGmailAssignment.org_id == context.organization_id,
        )
    )
    if assignment is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The Gmail assignment was not found.",
        )
    db.delete(assignment)
    db.commit()


def _validate_admin_state(
    oauth_state: GoogleOAuthState,
    db: Session,
) -> User:
    admin = db.scalar(
        select(User).where(
            User.id == oauth_state.admin_user_id,
            User.organization_id == oauth_state.org_id,
            User.role == "admin",
            User.is_active.is_(True),
        )
    )
    if admin is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="The organization administrator in the OAuth state is no longer active.",
        )
    return admin


def _frontend_redirect(status_value: str) -> RedirectResponse:
    frontend_origin = settings.frontend_origins.split(",", maxsplit=1)[0].strip()
    response = RedirectResponse(
        f"{frontend_origin.rstrip('/')}/settings?gmail={status_value}",
        status_code=status.HTTP_303_SEE_OTHER,
    )
    response.delete_cookie(
        key=GMAIL_OAUTH_STATE_COOKIE,
        path=GMAIL_CALLBACK_PATH,
        secure=settings.environment == "production",
        httponly=True,
        samesite="lax",
    )
    return response
