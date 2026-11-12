"""store normalized domains for lead sources

Revision ID: b1d6e8c43a20
Revises: a6c5e1f308bd
Create Date: 2026-11-12 02:18:00.000000
"""

from typing import Sequence, Union
from urllib.parse import urlsplit

from alembic import op
import sqlalchemy as sa


revision: str = "b1d6e8c43a20"
down_revision: Union[str, Sequence[str], None] = "a6c5e1f308bd"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _source_domain(source_url: str) -> str:
    value = source_url.strip()
    if "://" not in value:
        value = f"//{value}"

    try:
        hostname = urlsplit(value).hostname
    except ValueError as exc:
        raise RuntimeError(
            f"Cannot determine a source domain from {source_url!r}."
        ) from exc

    if not hostname:
        raise RuntimeError(
            f"Cannot determine a source domain from {source_url!r}."
        )

    hostname = hostname.rstrip(".").lower()
    if hostname.startswith("www."):
        hostname = hostname[4:]
    if not hostname:
        raise RuntimeError(
            f"Cannot determine a source domain from {source_url!r}."
        )

    return hostname


def upgrade() -> None:
    op.add_column(
        "lead_sources",
        sa.Column("source_domain", sa.String(length=255), nullable=True),
    )

    lead_sources = sa.table(
        "lead_sources",
        sa.column("id", sa.Uuid(as_uuid=True)),
        sa.column("source_url", sa.String(length=2000)),
        sa.column("source_domain", sa.String(length=255)),
    )
    connection = op.get_bind()
    rows = connection.execute(
        sa.select(lead_sources.c.id, lead_sources.c.source_url)
    )
    for source_id, source_url in rows:
        connection.execute(
            sa.update(lead_sources)
            .where(lead_sources.c.id == source_id)
            .values(source_domain=_source_domain(source_url))
        )

    with op.batch_alter_table("lead_sources") as batch_op:
        batch_op.alter_column(
            "source_domain",
            existing_type=sa.String(length=255),
            nullable=False,
        )


def downgrade() -> None:
    op.drop_column("lead_sources", "source_domain")
