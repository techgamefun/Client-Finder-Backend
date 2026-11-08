"""add normalized company names and unique lead source URLs

Revision ID: 7d91ca4f2e18
Revises: 24b35a2febfe
Create Date: 2026-10-27 15:24:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.services.normalization import normalize_company_name


revision: str = "7d91ca4f2e18"
down_revision: Union[str, Sequence[str], None] = "24b35a2febfe"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "companies",
        sa.Column("normalized_name", sa.String(length=200), nullable=True),
    )

    companies = sa.table(
        "companies",
        sa.column("id", sa.Integer()),
        sa.column("name", sa.String(length=200)),
        sa.column("normalized_name", sa.String(length=200)),
    )
    connection = op.get_bind()
    for company_id, name in connection.execute(
        sa.select(companies.c.id, companies.c.name)
    ):
        connection.execute(
            companies.update()
            .where(companies.c.id == company_id)
            .values(normalized_name=normalize_company_name(name))
        )

    op.create_index(
        "ix_companies_normalized_name",
        "companies",
        ["normalized_name"],
        unique=False,
    )

    lead_sources = sa.table(
        "lead_sources",
        sa.column("source_url", sa.String(length=2000)),
    )
    duplicate_urls = connection.execute(
        sa.select(sa.func.count()).select_from(
            sa.select(lead_sources.c.source_url)
            .group_by(lead_sources.c.source_url)
            .having(sa.func.count() > 1)
            .subquery()
        )
    ).scalar_one()
    if duplicate_urls:
        raise RuntimeError(
            "Cannot add unique lead source URLs: duplicate source_url values "
            "exist in lead_sources. Resolve those duplicates, then rerun the "
            "migration."
        )

    op.drop_constraint(
        "uq_company_source_url",
        "lead_sources",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_lead_source_url",
        "lead_sources",
        ["source_url"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_lead_source_url",
        "lead_sources",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_company_source_url",
        "lead_sources",
        ["company_id", "source_url"],
    )
    op.drop_index("ix_companies_normalized_name", table_name="companies")
    op.drop_column("companies", "normalized_name")
