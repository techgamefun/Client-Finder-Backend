from contextvars import ContextVar, Token
from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class TenantContext:
    organization_id: UUID
    user_id: UUID | None
    email: str
    role: str


_tenant_context: ContextVar[TenantContext | None] = ContextVar(
    "tenant_context",
    default=None,
)


def get_tenant_context() -> TenantContext | None:
    return _tenant_context.get()


def set_tenant_context(context: TenantContext) -> Token:
    return _tenant_context.set(context)


def reset_tenant_context(token: Token) -> None:
    _tenant_context.reset(token)
