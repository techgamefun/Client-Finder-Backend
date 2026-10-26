
from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import settings


database_url = settings.database_url.replace(
    "postgresql://",
    "postgresql+psycopg://",
    1,
)


engine = create_engine(
    database_url,
    pool_pre_ping=True,
)


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