"""store website crawl site types

Revision ID: c83609f2e751
Revises: b1d6e8c43a20
Create Date: 2026-11-12 15:06:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c83609f2e751"
down_revision: Union[str, Sequence[str], None] = "b1d6e8c43a20"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "website_crawls",
        sa.Column("site_type", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("website_crawls", "site_type")
