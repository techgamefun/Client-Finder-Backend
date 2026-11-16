from typing import Annotated
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse

from app.api.deps import require_admin_context
from app.config import settings
from app.services.gmail import validate_gmail_token_encryption_configuration
from app.services.gmail_oauth import create_google_authorization_url
from app.tenant_context import TenantContext


router = APIRouter(prefix="/auth", tags=["Gmail OAuth"])
AdminContext = Annotated[TenantContext, Depends(require_admin_context)]
GMAIL_OAUTH_STATE_COOKIE = "gmail_oauth_nonce"
GMAIL_CALLBACK_PATH = "/"


@router.get("/google/login")
def start_google_oauth(
    request: Request,
    context: AdminContext,
    org_id: UUID = Query(...),
    admin_user_id: UUID = Query(...),
) -> RedirectResponse:
    if (
        context.user_id is None
        or context.organization_id != org_id
        or context.user_id != admin_user_id
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the signed-in organization administrator can connect Gmail.",
        )
    _validate_request_origin(request)

    validate_gmail_token_encryption_configuration()
    authorization_url, nonce = create_google_authorization_url(
        org_id=org_id,
        admin_user_id=admin_user_id,
    )
    response = RedirectResponse(authorization_url, status_code=302)
    response.set_cookie(
        key=GMAIL_OAUTH_STATE_COOKIE,
        value=nonce,
        max_age=600,
        path=GMAIL_CALLBACK_PATH,
        secure=settings.environment == "production",
        httponly=True,
        samesite="none",
    )
    return response


def _validate_request_origin(request: Request) -> None:
    allowed_origins = {
        origin.strip().rstrip("/")
        for origin in settings.frontend_origins.split(",")
        if origin.strip()
    }
    origin = request.headers.get("origin")
    if origin is not None:
        if origin.rstrip("/") not in allowed_origins:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Google OAuth must be started from the configured frontend.",
            )
        return

    referer = request.headers.get("referer")
    if referer is not None:
        parsed_referer = urlsplit(referer)
        referer_origin = f"{parsed_referer.scheme}://{parsed_referer.netloc}"
        if referer_origin in allowed_origins:
            return
    elif request.headers.get("sec-fetch-site") in {"same-site", "same-origin"}:
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Google OAuth must be started from the configured frontend.",
    )
