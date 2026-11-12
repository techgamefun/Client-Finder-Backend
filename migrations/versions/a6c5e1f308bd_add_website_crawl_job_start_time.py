"""track when website crawl jobs start

Revision ID: a6c5e1f308bd
Revises: 93b7d2f4a610
Create Date: 2026-11-12 01:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a6c5e1f308bd"
down_revision: Union[str, Sequence[str], None] = "93b7d2f4a610"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "website_crawl_jobs",
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("website_crawl_jobs", "started_at")
