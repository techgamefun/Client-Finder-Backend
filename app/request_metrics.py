from contextvars import ContextVar, Token
from typing import TypedDict


class RequestMetrics(TypedDict):
    sql_count: int
    sql_duration_ms: float
    db_checkout_duration_ms: float


_request_metrics: ContextVar[RequestMetrics | None] = ContextVar(
    "request_metrics",
    default=None,
)


def begin_request_metrics() -> tuple[RequestMetrics, Token]:
    metrics: RequestMetrics = {
        "sql_count": 0,
        "sql_duration_ms": 0.0,
        "db_checkout_duration_ms": 0.0,
    }
    return metrics, _request_metrics.set(metrics)


def reset_request_metrics(token: Token) -> None:
    _request_metrics.reset(token)


def get_request_metrics() -> RequestMetrics | None:
    return _request_metrics.get()


def record_database_checkout(duration_ms: float) -> None:
    metrics = get_request_metrics()
    if metrics is not None:
        metrics["db_checkout_duration_ms"] += duration_ms
