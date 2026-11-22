"""remove skipped website crawl job records

Revision ID: f8b4c6d2a109
Revises: f7c2d9a1b604
Create Date: 2026-11-23 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f8b4c6d2a109"
down_revision: Union[str, Sequence[str], None] = "f7c2d9a1b604"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        sa.text("DELETE FROM website_crawl_jobs WHERE status = 'skipped'")
    )
    op.drop_column("website_crawl_jobs", "skip_reason")


def downgrade() -> None:
    op.add_column(
        "website_crawl_jobs",
        sa.Column("skip_reason", sa.Text(), nullable=True),
    )
