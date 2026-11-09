"""save Google PageSpeed scans with company leads

Revision ID: c7a61f8290be
Revises: 5c81d2e7a4b9
Create Date: 2026-11-09 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c7a61f8290be"
down_revision: Union[str, Sequence[str], None] = "5c81d2e7a4b9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "pagespeed_scans",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("website_url", sa.String(length=2000), nullable=False),
        sa.Column("strategy", sa.String(length=10), nullable=False),
        sa.Column("category_scores", sa.JSON(), nullable=False),
        sa.Column("report", sa.JSON(), nullable=False),
        sa.Column(
            "scanned_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_pagespeed_scans_company_scanned_at",
        "pagespeed_scans",
        ["company_id", "scanned_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_pagespeed_scans_company_scanned_at",
        table_name="pagespeed_scans",
    )
    op.drop_table("pagespeed_scans")
