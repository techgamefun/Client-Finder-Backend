from fastapi import Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import sessionmaker

from app.database import SessionLocal
from app.services.auth import (
    ACCESS_TOKEN_COOKIE,
    validate_access_token,
)
from app.tenant_context import reset_tenant_context, set_tenant_context


SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


async def authenticate_request(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api/v1/") and request.method not in SAFE_METHODS:
        origin = request.headers.get("origin")
        allowed_origins = {
            value.strip()
            for value in request.app.state.frontend_origins
            if value.strip()
        }
        if origin is not None and origin not in allowed_origins:
            return JSONResponse(
                status_code=403,
                content={"detail": "Request origin is not allowed."},
            )

    is_public_unsubscribe_path = path in {
        "/api/v1/unsubscribe",
        "/api/v1/unsubscribe/one-click",
    }
    token = request.cookies.get(ACCESS_TOKEN_COOKIE)
    tenant_token = None
    context = None
    if token and not is_public_unsubscribe_path:
        session_factory: sessionmaker = getattr(
            request.app.state,
            "session_factory",
            SessionLocal,
        )
        with session_factory() as db:
            context = validate_access_token(token, db)

    is_auth_path = path.startswith("/api/v1/auth/")
    is_google_oauth_callback = path == "/api/v1/gmail/callback"
    if (
        path.startswith("/api/v1/")
        and request.method != "OPTIONS"
        and not is_auth_path
        and not is_google_oauth_callback
        and not is_public_unsubscribe_path
        and context is None
    ):
        return JSONResponse(
            status_code=401,
            content={"detail": "Authentication is required."},
            headers={"WWW-Authenticate": "Cookie"},
        )

    if context is not None:
        tenant_token = set_tenant_context(context)
    try:
        return await call_next(request)
    finally:
        if tenant_token is not None:
            reset_tenant_context(tenant_token)
