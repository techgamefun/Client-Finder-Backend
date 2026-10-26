
from logging.config import fileConfig

from alembic import context

from app.database import Base, engine
from app import models  # Registers our model classes


config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Tell Alembic which SQLAlchemy tables to compare with Neon.
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=engine.url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
