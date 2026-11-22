
from collections.abc import Generator
from time import perf_counter

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.request_metrics import record_database_checkout
from app.tenant_context import TenantContext, get_tenant_context


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()

    try:
        checkout_started_at = perf_counter()
        db.connection()
        record_database_checkout(
            (perf_counter() - checkout_started_at) * 1000,
        )
        yield db
    finally:
        db.close()


def require_authenticated_context() -> TenantContext:
    context = get_tenant_context()
    if context is None or context.user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication is required.",
        )
    return context


def require_admin_context() -> TenantContext:
    context = require_authenticated_context()
    if context.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Organization administrator privileges are required.",
        )
    return context
