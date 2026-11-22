from time import perf_counter

from fastapi import Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy.orm import sessionmaker
from starlette.concurrency import run_in_threadpool

from app.database import SessionLocal
from app.request_metrics import (
    RequestMetrics,
    begin_request_metrics,
    record_database_checkout,
    reset_request_metrics,
)
from app.services.auth import (
    ACCESS_TOKEN_COOKIE,
    validate_access_token,
)
from app.tenant_context import (
    TenantContext,
    reset_tenant_context,
    set_tenant_context,
)


SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


async def authenticate_request(request: Request, call_next):
    path = request.url.path
    is_api_request = path.startswith("/api/v1/")
    metrics: RequestMetrics | None = None
    metrics_token = None
    if is_api_request:
        metrics, metrics_token = begin_request_metrics()

    request_started_at = perf_counter()
    auth_duration_ms = 0.0
    tenant_token = None
    try:
        if is_api_request and request.method not in SAFE_METHODS:
            origin = request.headers.get("origin")
            allowed_origins = {
                value.strip()
                for value in request.app.state.frontend_origins
                if value.strip()
            }
            if origin is not None and origin not in allowed_origins:
                response = JSONResponse(
                    status_code=403,
                    content={"detail": "Request origin is not allowed."},
                )
                return _add_server_timing(
                    response,
                    metrics,
                    auth_duration_ms,
                    request_started_at,
                )

        is_public_unsubscribe_path = path in {
            "/api/v1/unsubscribe",
            "/api/v1/unsubscribe/one-click",
        }
        token = request.cookies.get(ACCESS_TOKEN_COOKIE)
        context = None
        if token and not is_public_unsubscribe_path:
            session_factory: sessionmaker = getattr(
                request.app.state,
                "session_factory",
                SessionLocal,
            )
            auth_started_at = perf_counter()
            context = await run_in_threadpool(
                _validate_access_token,
                session_factory,
                token,
            )
            auth_duration_ms = (perf_counter() - auth_started_at) * 1000

        is_auth_path = path.startswith("/api/v1/auth/")
        is_google_oauth_callback = path == "/api/v1/gmail/callback"
        if (
            is_api_request
            and request.method != "OPTIONS"
            and not is_auth_path
            and not is_google_oauth_callback
            and not is_public_unsubscribe_path
            and context is None
        ):
            response = JSONResponse(
                status_code=401,
                content={"detail": "Authentication is required."},
                headers={"WWW-Authenticate": "Cookie"},
            )
        else:
            if context is not None:
                tenant_token = set_tenant_context(context)
            response = await call_next(request)
        return _add_server_timing(
            response,
            metrics,
            auth_duration_ms,
            request_started_at,
        )
    finally:
        if tenant_token is not None:
            reset_tenant_context(tenant_token)
        if metrics_token is not None:
            reset_request_metrics(metrics_token)


def _validate_access_token(
    session_factory: sessionmaker,
    token: str,
) -> TenantContext | None:
    with session_factory() as db:
        checkout_started_at = perf_counter()
        db.connection()
        record_database_checkout(
            (perf_counter() - checkout_started_at) * 1000,
        )
        return validate_access_token(token, db)


def _add_server_timing(
    response: Response,
    metrics: RequestMetrics | None,
    auth_duration_ms: float,
    request_started_at: float,
) -> Response:
    if metrics is None:
        return response
    total_duration_ms = (perf_counter() - request_started_at) * 1000
    app_duration_ms = max(0.0, total_duration_ms - auth_duration_ms)
    response.headers["Server-Timing"] = (
        f"auth;dur={auth_duration_ms:.1f}, "
        f"db-checkout;dur={metrics['db_checkout_duration_ms']:.1f}, "
        f"db;dur={metrics['sql_duration_ms']:.1f};"
        f"desc=\"{metrics['sql_count']} queries\", "
        f"app;dur={app_duration_ms:.1f}, "
        f"total;dur={total_duration_ms:.1f}"
    )
    return response
