"""add manual website crawl source

Revision ID: d24a11f9c830
Revises: c2d7a4e91b60
Create Date: 2026-11-24 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d24a11f9c830"
down_revision: Union[str, Sequence[str], None] = "c2d7a4e91b60"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "website_crawls",
        sa.Column(
            "source",
            sa.String(length=20),
            server_default="crawler",
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "ck_website_crawls_source_allowed",
        "website_crawls",
        "source IN ('crawler', 'manual')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_website_crawls_source_allowed",
        "website_crawls",
        type_="check",
    )
    op.drop_column("website_crawls", "source")
