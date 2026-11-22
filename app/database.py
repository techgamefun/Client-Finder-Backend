
import time

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import settings
from app.request_metrics import get_request_metrics


database_url = settings.database_url.replace(
    "postgresql://",
    "postgresql+psycopg://",
    1,
)


engine = create_engine(
    database_url,
    pool_pre_ping=True,
)


@event.listens_for(engine, "before_cursor_execute")
def _start_request_sql_timer(
    connection,
    cursor,
    statement,
    parameters,
    execution_context,
    executemany,
) -> None:
    if get_request_metrics() is None:
        return
    connection.info.setdefault("_request_sql_start_times", []).append(
        time.perf_counter(),
    )


@event.listens_for(engine, "after_cursor_execute")
def _record_request_sql_time(
    connection,
    cursor,
    statement,
    parameters,
    execution_context,
    executemany,
) -> None:
    start_times = connection.info.get("_request_sql_start_times")
    if not start_times:
        return
    started_at = start_times.pop()
    metrics = get_request_metrics()
    if metrics is None:
        return
    metrics["sql_count"] += 1
    metrics["sql_duration_ms"] += (time.perf_counter() - started_at) * 1000


class Base(DeclarativeBase):
    """Base class for all database models."""
    pass


SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    expire_on_commit=False,
)


def check_database_connection() -> bool:
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))

    return True